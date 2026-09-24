import asyncio

import discord
import logfire
from discord.ext import commands

from repodrop.announcer.dispatcher import Dispatcher
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
        self.dispatcher = Dispatcher(settings, self.sessions, self, self.announcer_wake)
        self._background: list[asyncio.Task[None]] = []

    async def setup_hook(self) -> None:
        await self.add_cog(SubscriptionsCog(self))
        self._background = [
            asyncio.create_task(self.poller.run(), name="poller"),
            asyncio.create_task(self.poller.run_maintenance(), name="maintenance"),
            asyncio.create_task(self.dispatcher.run(), name="announcer"),
        ]
        for task in self._background:
            task.add_done_callback(_log_task_exit)
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        try:
            if self.settings.dev_guild_id:
                guild = discord.Object(id=self.settings.dev_guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            else:
                synced = await self.tree.sync()
        except discord.Forbidden:
            # Keep running (polling and announcing still work); commands just aren't registered.
            logfire.error(
                "could not register slash commands in guild {guild_id}: the bot isn't in that "
                "server, or was invited without the applications.commands scope",
                guild_id=self.settings.dev_guild_id,
            )
            return
        logfire.info("synced {count} application commands", count=len(synced))

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
