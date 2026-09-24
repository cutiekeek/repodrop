import asyncio

import discord
import logfire
from discord.ext import commands

from repodrop.announcer.dispatcher import Dispatcher
from repodrop.bot.cogs.owner import OwnerCog
from repodrop.bot.cogs.subscriptions import SubscriptionsCog
from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.session import create_engine, create_session_factory
from repodrop.github.client import GitHubClient
from repodrop.guild_settings import GuildSettingsCache
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

    async def on_ready(self) -> None:
        logfire.info(
            "logged in as {user} in {guilds} guilds", user=str(self.user), guilds=len(self.guilds)
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        with logfire.span("guild_remove", guild_id=guild.id):
            async with self.sessions.begin() as session:
                deleted = await queries.delete_guild_subscriptions(session, guild.id)
                await queries.delete_guild_settings_unless_blocked(session, guild.id)
                await queries.prune_orphans(session)
            self.guild_settings.invalidate(guild.id)
            logfire.info(
                "removed from guild {guild_id}; deleted {count} subscriptions",
                guild_id=guild.id,
                count=deleted,
            )

    async def close(self) -> None:
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
