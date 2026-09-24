from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from repodrop.bot.welcome import pick_welcome_channel, welcome_message
from repodrop.config import Settings
from repodrop.guild_settings import EffectiveSettings

GUILD = 968928694154436658
DOCS = "https://github.com/cutiekeek/repodrop#getting-started"


def channel(id, position, *, can_post=True, embeds=True):
    perms = discord.Permissions(view_channel=can_post, send_messages=can_post, embed_links=embeds)
    return SimpleNamespace(
        id=id, position=position, permissions_for=lambda member: perms, send=AsyncMock()
    )


def guild(system=None, text=()):
    return SimpleNamespace(
        id=GUILD,
        name="Test",
        member_count=3,
        me=object(),
        system_channel=system,
        text_channels=list(text),
    )


def settings(**kw):
    return EffectiveSettings(**{"guild_id": GUILD, "max_repos": 25, "max_subscriptions": 100, **kw})


# --------------------------------------------------------------------------- channel choice


def test_prefers_the_system_channel():
    system = channel(1, 5)
    assert pick_welcome_channel(guild(system, [channel(2, 0), system])) is system


def test_falls_back_to_first_postable_channel_by_position():
    system = channel(1, 0, can_post=False)
    no_embeds = channel(2, 1, embeds=False)
    later, first_ok = channel(4, 9), channel(3, 2)
    picked = pick_welcome_channel(guild(system, [later, no_embeds, first_ok, system]))
    assert picked is first_ok


def test_no_postable_channel():
    assert pick_welcome_channel(guild(None, [channel(1, 0, can_post=False)])) is None


# --------------------------------------------------------------------------- message


async def test_welcome_message_content_and_button():
    embed, view = welcome_message(settings(max_repos=10, max_subscriptions=40), DOCS)
    text = " ".join(f.value for f in embed.fields)
    assert "/repodrop subscribe repo:owner/name" in text
    assert "/repodrop settings" in text and "/repodrop list" in text
    assert "10 repos across 40 subscriptions" in embed.footer.text
    assert [(b.label, b.url) for b in view.children] == [("Getting started", DOCS)]
    # No docs link configured: no button.
    assert welcome_message(settings(), None)[1] is None


# --------------------------------------------------------------------------- on join


async def make_bot():
    from repodrop.bot.client import RepoDropBot

    return RepoDropBot(Settings(discord_token="x", github_token="x", _env_file=None))


async def test_join_posts_welcome_without_mentions():
    bot = await make_bot()
    try:
        bot.guild_settings.get = AsyncMock(return_value=settings())
        system = channel(1, 0)
        await bot.on_guild_join(guild(system, [system]))
        system.send.assert_awaited_once()
        kwargs = system.send.await_args.kwargs
        assert kwargs["embed"].title == "Thanks for adding RepoDrop!"
        assert kwargs["allowed_mentions"].everyone is False
        assert kwargs["view"].children[0].url == DOCS
    finally:
        await bot.github.aclose()
        await bot.engine.dispose()


async def test_blocked_server_gets_no_welcome():
    bot = await make_bot()
    try:
        bot.guild_settings.get = AsyncMock(return_value=settings(blocked=True))
        system = channel(1, 0)
        await bot.on_guild_join(guild(system, [system]))
        system.send.assert_not_awaited()
    finally:
        await bot.github.aclose()
        await bot.engine.dispose()


async def test_failed_welcome_does_not_break_the_join():
    bot = await make_bot()
    try:
        bot.guild_settings.get = AsyncMock(return_value=settings())
        system = channel(1, 0)
        system.send.side_effect = discord.HTTPException(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Access"
        )
        await bot.on_guild_join(guild(system, [system]))  # logged, not raised
    finally:
        await bot.github.aclose()
        await bot.engine.dispose()
