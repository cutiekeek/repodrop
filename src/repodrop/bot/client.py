import asyncio

import discord
import logfire
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func

from repodrop.announcer.dispatcher import Dispatcher
from repodrop.bot.cogs.owner import OwnerCog
from repodrop.bot.cogs.subscriptions import SubscriptionsCog
from repodrop.bot.welcome import pick_welcome_channel, welcome_back_message, welcome_message
from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.session import create_engine, create_session_factory
from repodrop.github.client import GitHubClient
from repodrop.guild_settings import GuildSettingsCache
from repodrop.observability import audit, option_values
from repodrop.poller.scheduler import Poller


class RepoDropBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.none()
        intents.guilds = True  # guild join/leave and channel cache; no message content needed
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.engine = create_engine(settings.database_url)
        self.sessions = create_session_factory(self.engine)
        self.github = GitHubClient(settings.github_token.get_secret_value())
        self.guild_settings = GuildSettingsCache(settings, self.sessions)
        self.announcer_wake = asyncio.Event()
        self.poller = Poller(settings, self.sessions, self.github, self.announcer_wake)
        self.dispatcher = Dispatcher(
            settings, self.sessions, self, self.announcer_wake, self.guild_settings
        )
        self._background: list[asyncio.Task[None]] = []

    async def setup_hook(self) -> None:
        self._log_config()
        await self.add_cog(SubscriptionsCog(self))
        if self.settings.dev_guild_id:
            # Operator commands exist only in the operator's own server.
            await self.add_cog(OwnerCog(self), guild=discord.Object(self.settings.dev_guild_id))
        else:
            logfire.warn("DEV_GUILD_ID isn't set, so /repodrop-owner commands aren't registered")
        if not self.settings.owner_ids:
            logfire.warn("OWNER_IDS is empty, so nobody can run /repodrop-owner commands")
        self._background = [
            asyncio.create_task(self.poller.run(), name="poller"),
            asyncio.create_task(self.poller.run_maintenance(), name="maintenance"),
            asyncio.create_task(self.dispatcher.run(), name="announcer"),
        ]
        for task in self._background:
            task.add_done_callback(_log_task_exit)
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """Register /repodrop globally and /repodrop-owner in the operator's server.

        With DEV_SYNC, /repodrop is registered in DEV_GUILD_ID instead of globally, for instant
        updates while developing.
        """
        dev_guild = (
            discord.Object(self.settings.dev_guild_id) if self.settings.dev_guild_id else None
        )
        if self.settings.dev_sync and dev_guild is not None:
            self.tree.copy_global_to(guild=dev_guild)
        else:
            synced = await self.tree.sync()
            logfire.info("synced {count} global commands", count=len(synced))
        if dev_guild is None:
            return
        try:
            synced = await self.tree.sync(guild=dev_guild)
        except discord.Forbidden:
            # Keep running (polling and announcing still work); commands just aren't registered.
            logfire.error(
                "could not register commands in guild {guild_id}: the bot isn't in that "
                "server, or was invited without the applications.commands scope",
                guild_id=dev_guild.id,
            )
            return
        logfire.info(
            "synced {count} commands in guild {guild_id}", count=len(synced), guild_id=dev_guild.id
        )

    def _log_config(self) -> None:
        """What this process is running with (never secrets), for reading logs later."""
        s = self.settings
        logfire.info(
            "starting repodrop in {environment}",
            environment=s.environment,
            dev_guild_id=s.dev_guild_id,
            dev_sync=s.dev_sync,
            owner_count=len(s.owner_ids),
            max_repos_per_guild=s.max_repos_per_guild,
            max_subs_per_guild=s.max_subs_per_guild,
            max_branches_per_sub=s.max_branches_per_sub,
            poll_min_seconds=s.poll_min_interval.total_seconds(),
            poll_max_seconds=s.poll_max_interval.total_seconds(),
            poll_concurrency=s.poll_concurrency,
        )

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> None:
        """One log line per successful command: who ran what, where, with which options."""
        logfire.info(
            "/{command} by {user_id} in {guild_id}",
            command=command.qualified_name,
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            options=option_values(interaction.namespace),
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        settings = await self.guild_settings.get(guild.id)
        restored = await self._restore_guild(guild.id) if settings.left_at else None
        audit(
            "rejoined guild {guild_id} ({guild_name}); restored {restored} subscriptions"
            if restored is not None
            else "joined guild {guild_id} ({guild_name})",
            level="warn" if settings.blocked else "info",
            guild_id=guild.id,
            guild_name=guild.name,
            member_count=guild.member_count,
            blocked=settings.blocked,
            restored=restored,
        )
        if not settings.blocked:
            await self._welcome(guild, restored=restored)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Keep the server's setup for the grace period in case the bot is re-added."""
        with logfire.span("guild_remove", guild_id=guild.id):
            kept, skipped = await self._mark_departed(guild.id)
            audit(
                "removed from guild {guild_id} ({guild_name}); keeping {count} subscriptions "
                "for {grace_days} days",
                guild_id=guild.id,
                guild_name=guild.name,
                count=kept,
                grace_days=self.settings.guild_removal_grace.days,
                skipped_deliveries=skipped,
            )

    async def _mark_departed(self, guild_id: int) -> tuple[int, int]:
        """Mark a server as left and skip its queued announcements.

        Without the skip, those deliveries would fail with Forbidden and permanently disable
        the subscriptions, so nothing would come back on re-invite. Returns
        `(subscriptions_kept, deliveries_skipped)`.
        """
        await self.guild_settings.update(guild_id, left_at=func.now())
        async with self.sessions.begin() as session:
            kept = await queries.count_guild_subscriptions(session, guild_id)
            skipped = await queries.skip_guild_pending_deliveries(
                session, guild_id, "bot removed from server"
            )
        return kept, skipped

    async def _restore_guild(self, guild_id: int) -> int:
        """Clear a server's departure mark. Returns how many subscriptions it gets back."""
        await self.guild_settings.update(guild_id, left_at=None)
        async with self.sessions() as session:
            return await queries.count_guild_subscriptions(session, guild_id)

    async def _reconcile_guilds(self) -> None:
        """Catch joins and removals that happened while the bot was offline."""
        current = {g.id for g in self.guilds}
        async with self.sessions() as session:
            with_subs = await queries.guild_ids_with_subscriptions(session)
            departed = await queries.departed_guild_ids(session)
        for guild_id in sorted(with_subs - current - departed):
            kept, _ = await self._mark_departed(guild_id)
            audit(
                "removed from guild {guild_id} while offline; keeping {count} subscriptions",
                guild_id=guild_id,
                count=kept,
            )
        for guild_id in sorted(departed & current):
            restored = await self._restore_guild(guild_id)
            audit(
                "rejoined guild {guild_id} while offline; restored {restored} subscriptions",
                guild_id=guild_id,
                restored=restored,
            )

    async def _welcome(self, guild: discord.Guild, *, restored: int | None = None) -> None:
        """Post the getting-started message (or a welcome back), where the bot may post."""
        channel = pick_welcome_channel(guild)
        if channel is None:
            logfire.info(
                "no channel to post the welcome message in guild {guild_id}", guild_id=guild.id
            )
            return
        if restored is not None:
            embed, view = welcome_back_message(restored, self.settings.docs_url)
        else:
            embed, view = welcome_message(
                await self.guild_settings.get(guild.id), self.settings.docs_url
            )
        try:
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
                **({"view": view} if view is not None else {}),
            )
        except discord.HTTPException as exc:
            logfire.warn(
                "couldn't post the welcome message in guild {guild_id}: {error}",
                guild_id=guild.id,
                channel_id=channel.id,
                error=str(exc),
            )
            return
        logfire.info(
            "posted the welcome message in guild {guild_id}",
            guild_id=guild.id,
            channel_id=channel.id,
        )

    async def on_ready(self) -> None:
        logfire.info(
            "logged in as {user} in {guilds} guilds", user=str(self.user), guilds=len(self.guilds)
        )
        try:
            await self._reconcile_guilds()
        except Exception:
            logfire.exception("reconciling guild membership failed")

    async def close(self) -> None:
        logfire.info("shutting down")
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await super().close()
        await self.github.aclose()
        await self.engine.dispose()


def _log_task_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    if exc := task.exception():
        logfire.error("background task {name} crashed", name=task.get_name(), _exc_info=exc)
