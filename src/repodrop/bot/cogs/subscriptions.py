"""`/github` slash commands: subscribe, unsubscribe, list, test."""

from typing import TYPE_CHECKING, Any

import discord
import logfire
from discord import app_commands
from discord.ext import commands

from repodrop.announcer.embeds import release_embed
from repodrop.bot.checks import (
    AccessDenied,
    blocked_notice,
    can_modify,
    is_manager,
    manager_only,
    settings_for,
    subscriber_only,
)
from repodrop.db import queries
from repodrop.db.models import Kind
from repodrop.db.queries import WatchKey
from repodrop.github.client import GitHubError, NotFoundError, RateLimitedError
from repodrop.github.names import parse_repo
from repodrop.github.schemas import Release, Repository
from repodrop.guild_settings import cap_violation, usage_line
from repodrop.poller.detectors import RepoRef, release_payload

if TYPE_CHECKING:
    from repodrop.bot.client import RepoDropBot

EVENT_PRESETS: dict[str, list[str]] = {
    "releases": [Kind.RELEASE],
    "tags": [Kind.TAG],
    "commits": [Kind.COMMIT],
    "releases+tags": [Kind.RELEASE, Kind.TAG],
    "all": [Kind.RELEASE, Kind.TAG, Kind.COMMIT],
}
KIND_LABELS = {Kind.RELEASE: "releases", Kind.TAG: "tags", Kind.COMMIT: "commits"}
REQUIRED_PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
# What a member needs in a channel to point the bot at it (managers are exempt), so nobody can
# use the bot to post somewhere they can't post themselves.
MEMBER_PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True)


class UserError(Exception):
    """Shown to the invoking user as-is."""


# No default_permissions: Discord can't gate subcommands individually, so the group is visible to
# everyone and access is enforced per command (see bot/checks.py).
@app_commands.guild_only()
class SubscriptionsCog(
    commands.GroupCog, group_name="github", group_description="Announce GitHub repo updates"
):
    def __init__(self, bot: "RepoDropBot") -> None:
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Runs before every /github subcommand: blocked servers only get the block notice."""
        settings = await settings_for(interaction)
        if settings.blocked:
            raise AccessDenied(blocked_notice(settings, self.bot.settings.block_contact_url))
        return True

    # ------------------------------------------------------------------ /github subscribe

    @app_commands.command(description="Announce a public GitHub repo's updates in a channel")
    @app_commands.describe(
        repo="owner/name or a github.com URL",
        events="Which updates to announce (default: releases)",
        channel="Where to post (default: this channel)",
        prereleases="Also announce pre-releases (default: no)",
        branch="Branch to watch for commits (default: the repo's default branch)",
    )
    @app_commands.choices(
        events=[
            app_commands.Choice(name="Releases", value="releases"),
            app_commands.Choice(name="Tags", value="tags"),
            app_commands.Choice(name="Commits", value="commits"),
            app_commands.Choice(name="Releases + tags", value="releases+tags"),
            app_commands.Choice(name="Everything", value="all"),
        ]
    )
    @subscriber_only()
    @app_commands.checks.cooldown(5, 60, key=lambda i: (i.guild_id, i.user.id))
    async def subscribe(
        self,
        interaction: discord.Interaction,
        repo: str,
        events: str = "releases",
        channel: discord.TextChannel | None = None,
        prereleases: bool = False,
        branch: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        assert interaction.guild is not None
        target = self._target_channel(interaction, channel)
        self._check_bot_permissions(target)
        settings = await settings_for(interaction)
        member = interaction.user
        assert isinstance(member, discord.Member)
        if not is_manager(member, settings):
            self._check_member_permissions(target, member)

        gh_repo = await self._resolve_repo(repo)
        kinds = EVENT_PRESETS[events]
        if Kind.COMMIT not in kinds:
            branch = None
        elif branch is not None:
            branch = branch.strip()
            if branch == gh_repo.default_branch:
                branch = None  # track the default branch even if it's renamed later
            elif not await self.bot.github.branch_exists(gh_repo.full_name, branch):
                raise UserError(f"Branch `{branch}` doesn't exist in **{gh_repo.full_name}**.")

        async with self.bot.sessions.begin() as session:
            # Serialize subscribes per server so concurrent ones can't race past the caps.
            await queries.lock_guild(session, interaction.guild.id)
            db_repo = await queries.upsert_repo(
                session,
                github_id=gh_repo.id,
                full_name=gh_repo.full_name,
                default_branch=gh_repo.default_branch,
            )
            existing = await queries.get_subscription(
                session, guild_id=interaction.guild.id, channel_id=target.id, repo_id=db_repo.id
            )
            if existing is not None:
                if not can_modify(existing.created_by, member, settings):
                    raise UserError(
                        f"{target.mention} is already subscribed to **{gh_repo.full_name}**. "
                        f"Only <@{existing.created_by}>, who added it, or a manager can change it."
                    )
            else:
                # Updates and reactivations never hit the caps: disabled subscriptions count.
                usage = await queries.guild_usage(session, interaction.guild.id)
                followed = await queries.guild_follows_repo(
                    session, interaction.guild.id, db_repo.id
                )
                if problem := cap_violation(usage, settings, repo_already_followed=followed):
                    raise UserError(problem)
            _, created = await queries.upsert_subscription(
                session,
                guild_id=interaction.guild.id,
                channel_id=target.id,
                repo_id=db_repo.id,
                kinds=list(kinds),
                branch=branch,
                include_prereleases=prereleases,
                created_by=interaction.user.id,
            )
            keys = [
                WatchKey(
                    db_repo.id,
                    kind,
                    (branch or gh_repo.default_branch) if kind == Kind.COMMIT else "",
                )
                for kind in kinds
            ]
            for key in keys:
                await queries.ensure_watch(
                    session, key, initial_interval=self.bot.settings.poll_default_interval
                )
            if not created:
                # Settings may have changed (e.g. dropped commits); remove watches nobody needs.
                await queries.prune_orphans(session)
            usage = await queries.guild_usage(session, interaction.guild.id)

        latest_release = await self._baseline(keys, gh_repo)

        verb = "Subscribed" if created else "Updated the subscription for"
        what = ", ".join(KIND_LABELS[Kind(k)] for k in kinds)
        lines = [
            f"{verb} {target.mention} to **[{gh_repo.full_name}](<{gh_repo.html_url}>)** ({what})."
        ]
        if branch:
            lines.append(f"Watching commits on `{branch}`.")
        if prereleases and Kind.RELEASE in kinds:
            lines.append("Pre-releases will be announced too.")
        if Kind.RELEASE in kinds:
            if latest_release:
                lines.append(
                    f"Latest release: [{latest_release.name or latest_release.tag_name}]"
                    f"(<{latest_release.html_url}>). New releases will be posted from now on."
                )
            else:
                lines.append("This repo has no releases yet.")
        if created:
            lines.append(f"-# {usage_line(usage, settings)}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    async def _baseline(self, keys: list[WatchKey], gh_repo: Repository) -> Release | None:
        """Baseline new watches so subscribers aren't flooded with old items.

        Returns the latest published release, if releases are watched, for the preview.
        """
        latest: Release | None = None
        for key in keys:
            async with self.bot.sessions() as session:
                watch = await queries.get_watch(session, key)
            items: list[Any] | None = None
            if watch is not None and watch.last_seen_id is None:
                try:
                    items = await self.bot.poller.baseline(key, gh_repo.full_name)
                except GitHubError:
                    # The poller baselines any watch that still lacks a watermark.
                    logfire.warn("baseline failed for {repo}", repo=gh_repo.full_name)
            if key.kind == Kind.RELEASE:
                if items is None:
                    try:
                        items = (await self.bot.github.list_releases(gh_repo.full_name)).items
                    except GitHubError:
                        items = []
                latest = _latest_release(items or [])
        return latest

    # ------------------------------------------------------------------ /github unsubscribe

    @app_commands.command(description="Stop announcing a repo in a channel")
    @app_commands.describe(
        repo="The subscribed repo", channel="Channel to remove it from (default: this channel)"
    )
    async def unsubscribe(
        self,
        interaction: discord.Interaction,
        repo: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = self._target_channel(interaction, channel)
        full_name = parse_repo(repo) or repo.strip()
        settings = await settings_for(interaction)
        async with self.bot.sessions.begin() as session:
            sub = await queries.find_subscription_by_repo_name(
                session, guild_id=interaction.guild.id, channel_id=target.id, full_name=full_name
            )
            if sub is None:
                raise UserError(f"{target.mention} isn't subscribed to **{full_name}**.")
            if not can_modify(sub.created_by, interaction.user, settings):  # type: ignore[arg-type]
                raise UserError(
                    f"Only <@{sub.created_by}>, who added this subscription, or a manager can "
                    "remove it."
                )
            await queries.delete_subscription(session, sub.id)
            await queries.prune_orphans(session)
        await interaction.response.send_message(
            f"Unsubscribed {target.mention} from **{full_name}**.", ephemeral=True
        )

    # ------------------------------------------------------------------ /github list

    @app_commands.command(name="list", description="List this server's repo subscriptions")
    @app_commands.describe(channel="Only show subscriptions for this channel")
    async def list_(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        assert interaction.guild is not None
        settings = await settings_for(interaction)
        async with self.bot.sessions() as session:
            rows = await queries.list_subscriptions(
                session, interaction.guild.id, channel.id if channel else None
            )
            usage = await queries.guild_usage(session, interaction.guild.id)
        if not rows:
            where = channel.mention if channel else "This server"
            await interaction.response.send_message(
                f"{where} has no subscriptions. Add one with `/github subscribe`.", ephemeral=True
            )
            return

        lines = []
        for sub, repo in rows:
            kinds = ", ".join(KIND_LABELS.get(Kind(k), k) for k in sub.kinds)
            extras = []
            if sub.branch:
                extras.append(f"branch `{sub.branch}`")
            if sub.include_prereleases:
                extras.append("pre-releases")
            if not sub.active:
                extras.append("⚠️ paused: lost access to channel, run subscribe again to resume")
            suffix = f" · {' · '.join(extras)}" if extras else ""
            link = f"[{repo.full_name}](<https://github.com/{repo.full_name}>)"
            lines.append(f"<#{sub.channel_id}> · {link} · {kinds}{suffix}")
        embed = discord.Embed(title="GitHub subscriptions", description="\n".join(lines)[:4096])
        embed.set_footer(text=usage_line(usage, settings))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ /github test

    @app_commands.command(
        description="Post a repo's latest release to check formatting and permissions"
    )
    @app_commands.describe(
        repo="owner/name or a github.com URL", channel="Where to post (default: this channel)"
    )
    @manager_only()
    async def test(
        self,
        interaction: discord.Interaction,
        repo: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        target = self._target_channel(interaction, channel)
        self._check_bot_permissions(target)
        gh_repo = await self._resolve_repo(repo)
        result = await self.bot.github.list_releases(gh_repo.full_name)
        release = _latest_release(result.items or [])
        if release is None:
            raise UserError(f"**{gh_repo.full_name}** has no published releases to test with.")

        embed = release_embed(release_payload(RepoRef(gh_repo.full_name), release))
        try:
            await target.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden as exc:
            raise UserError(f"I couldn't post in {target.mention}: {exc.text}") from exc
        await interaction.followup.send(
            f"Posted a test announcement in {target.mention}.", ephemeral=True
        )

    # ------------------------------------------------------------------ autocomplete

    @unsubscribe.autocomplete("repo")
    async def _unsubscribe_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Managers see every subscribed repo; other members only the ones they added."""
        return await self._repo_choices(interaction, current, own_only=True)

    @test.autocomplete("repo")
    async def _test_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._repo_choices(interaction, current, own_only=False)

    async def _repo_choices(
        self, interaction: discord.Interaction, current: str, *, own_only: bool
    ) -> list[app_commands.Choice[str]]:
        # Autocomplete doesn't go through interaction_check, so recheck the block here.
        if interaction.guild is None:
            return []
        settings = await settings_for(interaction)
        if settings.blocked:
            return []
        created_by = None
        if own_only and not is_manager(interaction.user, settings):  # type: ignore[arg-type]
            created_by = interaction.user.id
        async with self.bot.sessions() as session:
            names = await queries.search_guild_repo_names(
                session, interaction.guild.id, current, created_by=created_by
            )
        return [app_commands.Choice(name=n, value=n) for n in names]

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _target_channel(
        interaction: discord.Interaction, channel: discord.TextChannel | None
    ) -> discord.TextChannel:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            raise UserError("Pick a text or announcement channel with the `channel` option.")
        return target

    @staticmethod
    def _check_bot_permissions(channel: discord.TextChannel) -> None:
        have = channel.permissions_for(channel.guild.me)
        missing = [
            name.replace("_", " ").title()
            for name, needed in REQUIRED_PERMISSIONS
            if needed and not getattr(have, name)
        ]
        if missing:
            raise UserError(f"I'm missing {', '.join(missing)} in {channel.mention}.")

    @staticmethod
    def _check_member_permissions(channel: discord.TextChannel, member: discord.Member) -> None:
        have = channel.permissions_for(member)
        if not all(getattr(have, name) for name, needed in MEMBER_PERMISSIONS if needed):
            raise UserError(
                f"You need to be able to view and send messages in {channel.mention} to "
                "subscribe it to a repo."
            )

    async def _resolve_repo(self, value: str) -> Repository:
        full_name = parse_repo(value)
        if full_name is None:
            raise UserError("Give the repo as `owner/name` or a github.com URL.")
        try:
            repo = await self.bot.github.get_repo(full_name)
        except NotFoundError:
            raise UserError(f"**{full_name}** doesn't exist or isn't public.") from None
        except RateLimitedError:
            raise UserError(
                "GitHub is rate limiting me right now; try again in a few minutes."
            ) from None
        except GitHubError:
            raise UserError("GitHub didn't respond properly; try again in a moment.") from None
        if repo.private:
            raise UserError(f"**{full_name}** is private; only public repos are supported.")
        return repo

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, UserError | AccessDenied):
            message = str(original)
        elif isinstance(original, app_commands.CommandOnCooldown):
            message = f"Slow down a little. Try again in {original.retry_after:.0f} seconds."
        else:
            logfire.exception(
                "command {command} failed",
                command=getattr(interaction.command, "qualified_name", None),
                _exc_info=original,
            )
            message = "Something went wrong on my end. Please try again."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def _latest_release(releases: list[Release]) -> Release | None:
    published = [r for r in releases if not r.draft and r.published_at is not None]
    return max(published, key=lambda r: r.published_at, default=None)  # type: ignore[arg-type, return-value]
