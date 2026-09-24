"""Phase 5: /repodrop-owner commands, the /repodrop settings panel, and command registration."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from sqlalchemy import select

from repodrop.bot.checks import AccessDenied
from repodrop.bot.cogs.owner import OwnerCog, parse_guild_id
from repodrop.bot.errors import UserError
from repodrop.bot.settings_panel import (
    LATEST_CHOICES,
    SettingsPanel,
    channel_problem,
    latest_choice,
    panel_embed,
    role_problem,
)
from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.models import Delivery
from repodrop.db.queries import WatchKey
from repodrop.guild_settings import EffectiveSettings, GuildSettingsCache

GUILD = 123456789012345678
OWNER = 42


def config(**kw) -> Settings:
    return Settings(discord_token="x", github_token="x", _env_file=None, **kw)


def effective(**kw) -> EffectiveSettings:
    return EffectiveSettings(**{"guild_id": GUILD, "max_repos": 25, "max_subscriptions": 100, **kw})


def fake_interaction(user_id=OWNER, **user):
    member = SimpleNamespace(
        id=user_id,
        guild_permissions=discord.Permissions(**user.get("perms", {})),
        roles=[SimpleNamespace(id=r) for r in user.get("roles", ())],
    )
    return SimpleNamespace(
        user=member,
        guild_id=GUILD,
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
    )


# --------------------------------------------------------------------------- /repodrop-owner


def test_parse_guild_id():
    assert parse_guild_id(f" {GUILD} ") == GUILD
    for bad in ("abc", "123", "-123456789012345678"):
        with pytest.raises(UserError):
            parse_guild_id(bad)


async def test_owner_commands_reject_non_owners():
    cog = OwnerCog(SimpleNamespace(settings=config(owner_ids=[OWNER])))
    assert await cog.interaction_check(fake_interaction(OWNER)) is True
    with pytest.raises(AccessDenied, match="operator"):
        await cog.interaction_check(fake_interaction(7))
    # Nobody is an owner when OWNER_IDS is empty.
    with pytest.raises(AccessDenied):
        await OwnerCog(SimpleNamespace(settings=config())).interaction_check(fake_interaction())


def owner_bot(sessions):
    settings = config(owner_ids=[OWNER])
    return SimpleNamespace(
        settings=settings,
        sessions=sessions,
        guild_settings=GuildSettingsCache(settings, sessions),
        get_guild=lambda gid: SimpleNamespace(name="Test Server") if gid == GUILD else None,
    )


def reply_text(interaction) -> str:
    return interaction.response.send_message.await_args.args[0]


async def test_owner_block_skips_pending_and_unblock_restores(sessions):
    bot = owner_bot(sessions)
    cog = OwnerCog(bot)
    async with sessions.begin() as s:
        repo = await queries.upsert_repo(s, github_id=1, full_name="o/r", default_branch="main")
        await queries.upsert_subscription(
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

    interaction = fake_interaction()
    await cog.block.callback(cog, interaction, str(GUILD), "spam")
    assert "Test Server" in reply_text(interaction) and "1 pending" in reply_text(interaction)
    settings = await bot.guild_settings.get(GUILD)
    assert settings.blocked and settings.blocked_reason == "spam"
    async with sessions() as s:
        delivery = (await s.scalars(select(Delivery))).one()
        assert delivery.status == "skipped"
        # Subscriptions are kept.
        assert (
            await queries.get_subscription(s, guild_id=GUILD, channel_id=1, repo_id=repo.id)
        ).active
        row = await queries.get_guild_settings(s, GUILD)
        assert row.blocked_at is not None

    await cog.unblock.callback(cog, fake_interaction(), str(GUILD))
    settings = await bot.guild_settings.get(GUILD)
    assert not settings.blocked and settings.blocked_reason is None


async def test_owner_limits_and_commits(sessions):
    bot = owner_bot(sessions)
    cog = OwnerCog(bot)
    await cog.limits.callback(cog, fake_interaction(), str(GUILD), max_repos=3)
    s = await bot.guild_settings.get(GUILD)
    assert (s.max_repos, s.max_subscriptions) == (3, 100)

    await cog.limits.callback(cog, fake_interaction(), str(GUILD), reset=True)
    s = await bot.guild_settings.get(GUILD)
    assert (s.max_repos, s.max_subscriptions) == (25, 100)

    with pytest.raises(UserError, match="Pass"):
        await cog.limits.callback(cog, fake_interaction(), str(GUILD))

    await cog.commits.callback(cog, fake_interaction(), str(GUILD), False)
    assert (await bot.guild_settings.get(GUILD)).commits_allowed is False


async def test_owner_inspect(sessions):
    bot = owner_bot(sessions)
    cog = OwnerCog(bot)
    await bot.guild_settings.update(GUILD, max_repos=40, blocked=True, blocked_reason="abuse")
    interaction = fake_interaction()
    await cog.inspect.callback(cog, interaction, str(GUILD))
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert "Test Server" in embed.title
    assert "Repo cap 40 (override; default 25)" in fields["Operator"]
    assert "**Blocked**: abuse" in fields["Operator"]
    assert fields["Recent delivery failures"] == "None"


# --------------------------------------------------------------------------- settings panel


def role(id=5, *, default=False, managed=False):
    return SimpleNamespace(id=id, name="R", is_default=lambda: default, managed=managed)


def test_role_problem():
    assert role_problem(None, purpose="manager") is None
    assert role_problem(role(), purpose="manager") is None
    assert "@everyone" in role_problem(role(default=True), purpose="manager")
    assert "integration" in role_problem(role(managed=True), purpose="subscriber")


def test_channel_problem_basics():
    assert channel_problem(None, MagicMock()) is None
    assert "text or announcement" in channel_problem(SimpleNamespace(), MagicMock())


def test_latest_choice_round_trip():
    for value, (_, access, public) in LATEST_CHOICES.items():
        assert latest_choice(effective(latest_access=access, latest_allow_public=public)) == value


def test_panel_embed_shows_values_and_operator_settings():
    embed = panel_embed(
        effective(manager_role_id=9, embed_style="compact", commits_allowed=False),
        "Repos 1/25 · Subscriptions 1/100",
    )
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Manager role"] == "<@&9>"
    assert fields["Subscriber role"].startswith("None")
    assert fields["Embed style"].startswith("Compact")
    assert "not allowed" in fields["Set by the bot operator"]


class FakeStore:
    def __init__(self, settings):
        self.settings = settings
        self.updates = []

    async def get(self, guild_id):
        return self.settings

    async def update(self, guild_id, **values):
        self.updates.append(values)
        self.settings = replace(self.settings, **values)
        return self.settings


async def test_panel_layout_fits_discord_limits():
    store = FakeStore(effective(subscriber_role_id=7))
    panel = SettingsPanel(
        store, store.settings, owner_id=1, can_change_manager_role=False, usage_text=""
    )
    rows = {item.row for item in panel.children}
    assert rows == {0, 1, 2, 3, 4}  # Discord's maximum of 5 rows
    manager_select = panel.children[0]
    assert isinstance(manager_select, discord.ui.RoleSelect) and manager_select.disabled
    assert [d.id for d in panel.children[1].default_values] == [7]


async def test_panel_saves_and_enforces_permissions():
    store = FakeStore(effective())
    panel = SettingsPanel(
        store, store.settings, owner_id=1, can_change_manager_role=True, usage_text=""
    )
    admin = fake_interaction(1, perms={"manage_guild": True})
    await panel._save(admin, embed_style="compact")
    assert store.updates == [{"embed_style": "compact"}]
    assert panel.settings.embed_style == "compact"
    admin.response.edit_message.assert_awaited_once()

    # A manager via the manager role can change settings, but not the manager role itself.
    store.settings = replace(store.settings, manager_role_id=77)
    role_manager = fake_interaction(1, roles=[77])
    await panel._save(role_manager, manager_role_id=None)
    assert len(store.updates) == 1
    notice = role_manager.response.edit_message.await_args.kwargs["embed"].description
    assert "Manage Server" in notice

    # Someone who stopped being a manager can't keep using an open panel.
    outsider = fake_interaction(1)
    await panel._save(outsider, embed_style="full")
    assert len(store.updates) == 1
    assert (
        "no longer a manager"
        in outsider.response.edit_message.await_args.kwargs["embed"].description
    )


async def test_panel_only_answers_its_opener():
    store = FakeStore(effective())
    panel = SettingsPanel(
        store, store.settings, owner_id=1, can_change_manager_role=True, usage_text=""
    )
    assert await panel.interaction_check(fake_interaction(1))
    assert not await panel.interaction_check(fake_interaction(2))


# --------------------------------------------------------------------------- registration


async def make_bot(**kw):
    from repodrop.bot.client import RepoDropBot
    from repodrop.bot.cogs.subscriptions import SubscriptionsCog

    bot = RepoDropBot(config(**kw))
    await bot.add_cog(SubscriptionsCog(bot))
    if bot.settings.dev_guild_id:
        await bot.add_cog(OwnerCog(bot), guild=discord.Object(bot.settings.dev_guild_id))
    bot.tree.sync = AsyncMock(return_value=[])
    return bot


async def close(bot):
    await bot.github.aclose()
    await bot.engine.dispose()


async def test_owner_commands_only_in_dev_guild_and_github_global():
    bot = await make_bot(dev_guild_id=GUILD)
    try:
        assert [c.name for c in bot.tree.get_commands()] == ["repodrop"]
        assert [c.name for c in bot.tree.get_commands(guild=discord.Object(GUILD))] == [
            "repodrop-owner"
        ]
        github = bot.tree.get_commands()[0]
        assert "settings" in [c.name for c in github.commands]
        # /repodrop is visible to everyone; /repodrop-owner only to Administrators.
        assert github.to_dict(bot.tree).get("default_member_permissions") is None
        [owner] = bot.tree.get_commands(guild=discord.Object(GUILD))
        admin = discord.Permissions(administrator=True).value
        assert int(owner.to_dict(bot.tree)["default_member_permissions"]) == admin

        await bot._sync_commands()
        calls = [c.kwargs.get("guild") for c in bot.tree.sync.await_args_list]
        assert calls[0] is None  # global sync for /repodrop
        assert calls[1].id == GUILD  # /repodrop-owner in the dev server
    finally:
        await close(bot)


async def test_dev_sync_puts_github_in_dev_guild_only():
    bot = await make_bot(dev_guild_id=GUILD, dev_sync=True)
    try:
        await bot._sync_commands()
        [call] = bot.tree.sync.await_args_list
        assert call.kwargs["guild"].id == GUILD
        names = {c.name for c in bot.tree.get_commands(guild=discord.Object(GUILD))}
        assert names == {"repodrop", "repodrop-owner"}
    finally:
        await close(bot)


async def test_no_dev_guild_means_global_only():
    bot = await make_bot()
    try:
        await bot._sync_commands()
        [call] = bot.tree.sync.await_args_list
        assert "guild" not in call.kwargs
    finally:
        await close(bot)
