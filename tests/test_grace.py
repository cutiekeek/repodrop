"""The grace period after RepoDrop is removed from a server."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from sqlalchemy import select

from repodrop.bot.client import RepoDropBot
from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.models import Delivery, Subscription
from repodrop.db.queries import WatchKey
from repodrop.guild_settings import GuildSettingsCache

GUILD = 968928694154436658


def make_bot(sessions) -> RepoDropBot:
    """A bot wired to the test schema instead of its own engine."""
    settings = Settings(discord_token="x", github_token="x", _env_file=None)
    bot = RepoDropBot(settings)
    bot.sessions = sessions
    bot.guild_settings = GuildSettingsCache(settings, sessions)
    return bot


async def close(bot):
    await bot.github.aclose()
    await bot.engine.dispose()


def postable_channel():
    perms = discord.Permissions(view_channel=True, send_messages=True, embed_links=True)
    return SimpleNamespace(id=1, position=0, permissions_for=lambda m: perms, send=AsyncMock())


def guild(channel=None):
    return SimpleNamespace(
        id=GUILD,
        name="Test",
        member_count=3,
        me=object(),
        system_channel=channel,
        text_channels=[channel] if channel else [],
    )


async def seed(sessions):
    """One active subscription with a queued announcement."""
    async with sessions.begin() as s:
        repo = await queries.upsert_repo(s, github_id=9, full_name="o/r", default_branch="main")
        sub, _ = await queries.upsert_subscription(
            s,
            guild_id=GUILD,
            channel_id=1,
            repo_id=repo.id,
            kinds=["release"],
            branches=[],
            include_prereleases=False,
            created_by=1,
        )
        event_id = await queries.insert_event(
            s, repo_id=repo.id, kind="release", external_id="1", payload={}
        )
        await queries.fan_out_event(
            s, event_id=event_id, key=WatchKey(repo.id, "release", ""), default_branch="main"
        )
    return sub


async def test_removal_keeps_data_and_rejoin_restores_it(sessions):
    sub = await seed(sessions)
    bot = make_bot(sessions)
    try:
        await bot.on_guild_remove(guild())
        settings = await bot.guild_settings.get(GUILD)
        assert settings.left_at is not None
        async with sessions() as s:
            kept = await s.get(Subscription, sub.id)
            assert kept.active  # kept as-is, not disabled
            # The queued announcement is skipped, so it can't fail and disable the subscription.
            delivery = (await s.scalars(select(Delivery))).one()
            assert delivery.status == "skipped" and delivery.last_error == "bot removed from server"

        channel = postable_channel()
        await bot.on_guild_join(guild(channel))
        assert (await bot.guild_settings.get(GUILD)).left_at is None
        embed = channel.send.await_args.kwargs["embed"]
        assert embed.title == "Welcome back!"
        assert "your 1 subscription and settings were restored" in embed.description
    finally:
        await close(bot)


async def test_first_join_gets_the_normal_welcome(sessions):
    bot = make_bot(sessions)
    try:
        channel = postable_channel()
        await bot.on_guild_join(guild(channel))
        assert channel.send.await_args.kwargs["embed"].title == "Thanks for adding RepoDrop!"
    finally:
        await close(bot)


async def test_reconcile_catches_changes_made_while_offline(sessions):
    await seed(sessions)
    bot = make_bot(sessions)
    try:
        # Offline removal: the server has subscriptions but isn't in the bot's guild list.
        bot._connection._guilds = {}
        await bot._reconcile_guilds()
        assert (await bot.guild_settings.get(GUILD)).left_at is not None

        # Offline re-add: the server is back in the guild list.
        bot._connection._guilds = {GUILD: SimpleNamespace(id=GUILD)}
        await bot._reconcile_guilds()
        assert (await bot.guild_settings.get(GUILD)).left_at is None
    finally:
        await close(bot)
