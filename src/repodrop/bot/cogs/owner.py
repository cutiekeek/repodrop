"""`/owner` commands: the operator's per-server controls.

Registered only in the operator's private server (DEV_GUILD_ID), and every command also checks
OWNER_IDS, so nobody else can see or run them.
"""

from typing import TYPE_CHECKING

import discord
import logfire
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func

from repodrop.announcer.embeds import truncate
from repodrop.bot.checks import AccessDenied, is_owner
from repodrop.bot.errors import UserError, reply_with_error
from repodrop.db import queries
from repodrop.guild_settings import EffectiveSettings, usage_line

if TYPE_CHECKING:
    from repodrop.bot.client import RepoDropBot

GuildIdParam = app_commands.describe(guild_id="Server ID (right-click the server → Copy Server ID)")
MAX_CAP = 10_000
FIELD_LIMIT = 1024  # characters per embed field value


def parse_guild_id(value: str) -> int:
    """Server IDs are taken as text: Discord snowflakes don't fit its integer option type."""
    value = value.strip()
    if not value.isdigit() or not 15 <= len(value) <= 21:
        raise UserError(f"`{value}` doesn't look like a server ID.")
    return int(value)


class OwnerCog(commands.GroupCog, group_name="owner", group_description="Operator controls"):
    def __init__(self, bot: "RepoDropBot") -> None:
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Runs before every /owner subcommand."""
        if not is_owner(interaction.user.id, self.bot.settings.owner_ids):
            raise AccessDenied("This command is only for the bot's operator.")
        return True

    # ------------------------------------------------------------------ limits

    @app_commands.command(description="Set or reset a server's repo and subscription caps")
    @GuildIdParam
    @app_commands.describe(
        max_repos="Distinct repos the server may follow",
        max_subscriptions="Total subscriptions (channel + repo pairs)",
        reset="Go back to the global defaults for both caps",
    )
    async def limits(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        max_repos: app_commands.Range[int, 0, MAX_CAP] | None = None,
        max_subscriptions: app_commands.Range[int, 0, MAX_CAP] | None = None,
        reset: bool = False,
    ) -> None:
        gid = parse_guild_id(guild_id)
        if reset:
            values = {"max_repos": None, "max_subscriptions": None}
        else:
            values = {
                k: v
                for k, v in (("max_repos", max_repos), ("max_subscriptions", max_subscriptions))
                if v is not None
            }
            if not values:
                raise UserError("Pass `max_repos`, `max_subscriptions`, or `reset:true`.")
        settings = await self.bot.guild_settings.update(gid, **values)
        # Lowering a cap below current usage deletes nothing; it only blocks new subscriptions.
        await self._reply(interaction, gid, f"Caps updated. {await self._usage(settings)}")

    # ------------------------------------------------------------------ commits

    @app_commands.command(description="Allow or disallow commit subscriptions in a server")
    @GuildIdParam
    async def commits(self, interaction: discord.Interaction, guild_id: str, allowed: bool) -> None:
        gid = parse_guild_id(guild_id)
        await self.bot.guild_settings.update(gid, commits_allowed=allowed)
        note = (
            "Commit announcements are allowed."
            if allowed
            else "Commit announcements are off. Existing commit subscriptions are kept and "
            "resume if you allow them again."
        )
        await self._reply(interaction, gid, note)

    # ------------------------------------------------------------------ block / unblock

    @app_commands.command(description="Block a server: every /github command shows the reason")
    @GuildIdParam
    @app_commands.describe(reason="Shown to the server's members")
    async def block(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        reason: app_commands.Range[str, 1, 300],
    ) -> None:
        gid = parse_guild_id(guild_id)
        await self.bot.guild_settings.update(
            gid, blocked=True, blocked_reason=reason, blocked_at=func.now()
        )
        async with self.bot.sessions.begin() as session:
            skipped = await queries.skip_guild_pending_deliveries(session, gid, "server blocked")
        logfire.warn("blocked guild {guild_id}: {reason}", guild_id=gid, reason=reason)
        await self._reply(
            interaction,
            gid,
            f"Blocked. {skipped} pending announcements were skipped. Subscriptions are kept, so "
            "unblocking restores the server's setup; the block also survives the bot being "
            "removed and re-invited.",
        )

    @app_commands.command(description="Lift a server's block")
    @GuildIdParam
    async def unblock(self, interaction: discord.Interaction, guild_id: str) -> None:
        gid = parse_guild_id(guild_id)
        await self.bot.guild_settings.update(
            gid, blocked=False, blocked_reason=None, blocked_at=None
        )
        logfire.info("unblocked guild {guild_id}", guild_id=gid)
        await self._reply(interaction, gid, "Unblocked. New announcements resume from now on.")

    # ------------------------------------------------------------------ inspect

    @app_commands.command(description="Show a server's settings, usage and recent failures")
    @GuildIdParam
    async def inspect(self, interaction: discord.Interaction, guild_id: str) -> None:
        gid = parse_guild_id(guild_id)
        settings = await self.bot.guild_settings.get(gid)
        async with self.bot.sessions() as session:
            row = await queries.get_guild_settings(session, gid)
            subs = await queries.list_subscriptions(session, gid)
            failures = await queries.recent_delivery_failures(session, gid)

        embed = discord.Embed(title=f"Server {self._guild_label(gid)}")
        config = self.bot.settings
        repos_cap = _cap(row.max_repos if row else None, config.max_repos_per_guild)
        subs_cap = _cap(row.max_subscriptions if row else None, config.max_subs_per_guild)
        active = sum(s.active for s, _ in subs)
        embed.add_field(
            name="Usage",
            value=f"{await self._usage(settings)}\n{active} active · {len(subs) - active} disabled",
            inline=False,
        )
        embed.add_field(
            name="Operator",
            value=(
                f"Repo cap {repos_cap} · subscription cap {subs_cap}\n"
                f"Commits {'allowed' if settings.commits_allowed else 'not allowed'}\n"
                + (
                    f"**Blocked**: {settings.blocked_reason or '(no reason)'}"
                    if settings.blocked
                    else "Not blocked"
                )
            ),
            inline=False,
        )
        embed.add_field(name="Admin settings", value=_admin_summary(settings), inline=False)
        failure_lines = "\n".join(
            f"<t:{int(f.detected_at.timestamp())}:R> **{f.repo_full_name}** {f.event_kind} "
            f"→ <#{f.channel_id}> · {f.status}: {(f.error or '')[:120]}"
            for f in failures
        )
        embed.add_field(
            name="Recent delivery failures",
            value=truncate(failure_lines, FIELD_LIMIT) or "None",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ helpers

    async def _usage(self, settings: EffectiveSettings) -> str:
        async with self.bot.sessions() as session:
            usage = await queries.guild_usage(session, settings.guild_id)
        return usage_line(usage, settings)

    def _guild_label(self, guild_id: int) -> str:
        guild = self.bot.get_guild(guild_id)
        return f"{guild.name} ({guild_id})" if guild else f"{guild_id} (bot not in server)"

    async def _reply(self, interaction: discord.Interaction, guild_id: int, text: str) -> None:
        await interaction.response.send_message(
            f"**{self._guild_label(guild_id)}**: {text}", ephemeral=True
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        await reply_with_error(interaction, error)


def _cap(override: int | None, default: int) -> str:
    return f"{override} (override; default {default})" if override is not None else f"{default}"


def _admin_summary(s: EffectiveSettings) -> str:
    def role(role_id: int | None, default: str) -> str:
        return f"<@&{role_id}>" if role_id else default

    return (
        f"Manager role: {role(s.manager_role_id, 'none (Manage Server only)')}\n"
        f"Subscriber role: {role(s.subscriber_role_id, 'none (everyone)')}\n"
        f"Default channel: {f'<#{s.default_channel_id}>' if s.default_channel_id else 'none'}\n"
        f"Embed style: {s.embed_style} · /github latest: {s.latest_access}"
        f"{'' if s.latest_allow_public else ', private only'}"
    )
