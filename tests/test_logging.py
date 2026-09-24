"""The logs people rely on: audit trail of changes, command usage, and system events."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from logfire.testing import CaptureLogfire

from repodrop.bot.checks import AccessDenied
from repodrop.bot.errors import UserError, reply_with_error
from repodrop.bot.settings_panel import SettingsPanel
from repodrop.config import Settings
from repodrop.guild_settings import EffectiveSettings
from repodrop.observability import audit, option_values

GUILD, USER, CHANNEL = 111, 222, 333


def logs(capfire: CaptureLogfire) -> list[dict]:
    """Captured log records as {msg, level, tags, attrs}."""
    records = []
    for span in capfire.exporter.exported_spans_as_dict():
        attrs = span["attributes"]
        if attrs.get("logfire.span_type") != "log":
            continue
        records.append(
            {
                "msg": attrs["logfire.msg"],
                "level": attrs.get("logfire.level_num"),
                "tags": list(attrs.get("logfire.tags", ())),
                "attrs": attrs,
            }
        )
    return records


def find(capfire: CaptureLogfire, fragment: str) -> dict:
    matches = [r for r in logs(capfire) if fragment in r["msg"]]
    assert matches, f"no log containing {fragment!r}; got {[r['msg'] for r in logs(capfire)]}"
    return matches[-1]


INFO, WARN = 9, 13  # logfire level numbers


# --------------------------------------------------------------------------- helpers


def test_audit_is_tagged_and_levelled(capfire: CaptureLogfire):
    audit("subscribed {channel_id} to {repo}", channel_id=CHANNEL, repo="o/r", user_id=USER)
    audit("operator blocked guild {guild_id}", level="warn", guild_id=GUILD)
    sub, block = logs(capfire)
    assert sub["msg"] == f"subscribed {CHANNEL} to o/r" and sub["tags"] == ["audit"]
    assert sub["level"] == INFO and sub["attrs"]["user_id"] == USER
    assert block["level"] == WARN and block["tags"] == ["audit"]


def test_option_values_turn_discord_objects_into_ids():
    channel = SimpleNamespace(id=CHANNEL, name="general")
    assert option_values(
        [("repo", "o/r"), ("channel", channel), ("public", True), ("branches", None)]
    ) == {"repo": "o/r", "channel": CHANNEL, "public": True, "branches": None}


# --------------------------------------------------------------------------- commands


def interaction(command="subscribe", **kw):
    return SimpleNamespace(
        command=SimpleNamespace(qualified_name=f"repodrop {command}"),
        user=SimpleNamespace(id=USER),
        guild_id=GUILD,
        channel_id=CHANNEL,
        namespace=kw.get("namespace", []),
        response=SimpleNamespace(is_done=lambda: False, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.parametrize(
    ("error", "refusal"),
    [
        (UserError("That repo doesn't exist."), "UserError"),
        (AccessDenied("Only managers can use this."), "AccessDenied"),
        (app_commands.CommandOnCooldown(app_commands.Cooldown(1, 60), 12.0), "CommandOnCooldown"),
    ],
)
async def test_refusals_are_logged_with_the_reason(capfire: CaptureLogfire, error, refusal):
    wrapped = (
        error
        if isinstance(error, app_commands.AppCommandError)
        else (app_commands.CommandInvokeError(SimpleNamespace(name="x", qualified_name="x"), error))
    )
    await reply_with_error(interaction(), wrapped)
    record = find(capfire, "refused for")
    assert record["level"] == INFO
    assert record["attrs"]["refusal"] == refusal
    assert record["attrs"]["command"] == "repodrop subscribe"
    assert record["attrs"]["guild_id"] == GUILD


async def test_unexpected_errors_are_logged_as_errors(capfire: CaptureLogfire):
    boom = app_commands.CommandInvokeError(
        SimpleNamespace(name="x", qualified_name="x"), RuntimeError("boom")
    )
    i = interaction()
    await reply_with_error(i, boom)
    record = find(capfire, "failed for")
    assert record["level"] >= 17  # error
    assert "Something went wrong" in i.response.send_message.await_args.args[0]


async def test_every_completed_command_is_logged_with_options(capfire: CaptureLogfire):
    from repodrop.bot.client import RepoDropBot

    bot = RepoDropBot(Settings(discord_token="x", github_token="x", _env_file=None))
    try:
        channel = SimpleNamespace(id=CHANNEL)
        i = interaction(namespace=[("repo", "o/r"), ("channel", channel)])
        await bot.on_app_command_completion(i, SimpleNamespace(qualified_name="repodrop subscribe"))
        record = find(capfire, "/repodrop subscribe by")
        assert record["attrs"]["user_id"] == USER and record["attrs"]["guild_id"] == GUILD
        assert '"channel":333' in record["attrs"]["options"].replace(" ", "")

        guild = SimpleNamespace(id=GUILD, name="Test", member_count=5)
        bot.guild_settings.get = AsyncMock(
            return_value=EffectiveSettings(GUILD, 25, 100, blocked=True)
        )
        await bot.on_guild_join(guild)
        joined = find(capfire, "joined guild")
        assert joined["tags"] == ["audit"] and joined["level"] == WARN  # a blocked server rejoined
    finally:
        await bot.github.aclose()
        await bot.engine.dispose()


# --------------------------------------------------------------------------- settings and operator


async def test_settings_changes_are_audited(capfire: CaptureLogfire):
    settings = EffectiveSettings(GUILD, 25, 100)

    class Store:
        async def get(self, guild_id):
            return settings

        async def update(self, guild_id, **values):
            return replace(settings, **values)

    panel = SettingsPanel(
        Store(), settings, owner_id=USER, can_change_manager_role=True, usage_text=""
    )
    admin = SimpleNamespace(
        id=USER,
        guild_permissions=discord.Permissions(manage_guild=True),
        roles=[],
    )
    i = SimpleNamespace(user=admin, response=SimpleNamespace(edit_message=AsyncMock()))
    await panel._save(i, embed_style="compact", subscriber_role_id=55)
    record = find(capfire, "changed server settings")
    assert record["tags"] == ["audit"] and record["attrs"]["user_id"] == USER
    assert '"subscriber_role_id":"55"' in record["attrs"]["changes"].replace(" ", "")


async def test_operator_actions_are_audited_with_who(capfire: CaptureLogfire, sessions):
    from repodrop.bot.cogs.owner import OwnerCog
    from repodrop.guild_settings import GuildSettingsCache

    config = Settings(discord_token="x", github_token="x", _env_file=None, owner_ids=[USER])
    bot = SimpleNamespace(
        settings=config,
        sessions=sessions,
        guild_settings=GuildSettingsCache(config, sessions),
        get_guild=lambda gid: None,
    )
    cog = OwnerCog(bot)
    gid = "123456789012345678"
    i = SimpleNamespace(
        user=SimpleNamespace(id=USER), response=SimpleNamespace(send_message=AsyncMock())
    )
    await cog.block.callback(cog, i, gid, "spam")
    await cog.unblock.callback(cog, i, gid)
    await cog.limits.callback(cog, i, gid, max_repos=3)
    await cog.commits.callback(cog, i, gid, False)

    blocked = find(capfire, "operator blocked guild")
    assert blocked["level"] == WARN and blocked["attrs"]["reason"] == "spam"
    for fragment in ("operator unblocked", "operator changed caps", "operator disallowed commits"):
        record = find(capfire, fragment)
        assert record["tags"] == ["audit"] and record["attrs"]["user_id"] == USER


# --------------------------------------------------------------------------- system events


async def test_permanent_delivery_failure_is_a_warning(capfire: CaptureLogfire, sessions):
    from repodrop.announcer.dispatcher import Dispatcher
    from tests.test_db import FakeChannel, FakeClient, http_error, seed_release_delivery
    from tests.test_db import settings as db_settings

    await seed_release_delivery(sessions)
    channel = FakeChannel(error=http_error(discord.HTTPException, 400))
    await Dispatcher(db_settings(), sessions, FakeClient(channel), asyncio.Event()).drain()
    record = find(capfire, "failed permanently")
    assert record["level"] == WARN and record["attrs"]["guild_id"] == 111


async def test_detected_events_are_logged(capfire: CaptureLogfire, sessions):
    from repodrop.db import queries
    from repodrop.db.queries import WatchKey
    from repodrop.poller.scheduler import Poller
    from tests.test_db import FakeGitHub, gh_release, make_sub
    from tests.test_db import settings as db_settings

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    gh = FakeGitHub()
    gh.releases = [gh_release(2, t0 + timedelta(hours=1)), gh_release(1, t0)]
    async with sessions.begin() as s:
        repo, _ = await make_sub(s)
        key = WatchKey(repo.id, "release", "")
        await queries.ensure_watch(s, key, initial_interval=timedelta(minutes=10))
        await queries.update_watch(
            s,
            key,
            etag=None,
            last_seen_id=t0.isoformat(),
            poll_interval=timedelta(minutes=10),
            next_poll_at=t0,
        )
    await Poller(db_settings(), sessions, gh, asyncio.Event()).run_cycle()
    record = find(capfire, "detected 1 new release")
    assert record["attrs"]["repo"] == "octo/widget" and record["attrs"]["deliveries"] == 1
