from types import SimpleNamespace

import discord
import pytest

from repodrop.bot.checks import (
    AccessDenied,
    blocked_notice,
    can_modify,
    has_subscribe_access,
    is_manager,
)
from repodrop.bot.cogs.subscriptions import SubscriptionsCog
from repodrop.config import Settings
from repodrop.db.models import GuildSettings
from repodrop.db.queries import GuildUsage
from repodrop.guild_settings import EffectiveSettings, cap_violation

MANAGER_ROLE, SUBSCRIBER_ROLE = 900, 901


def member(id=1, *, manage_guild=False, roles=()):
    return SimpleNamespace(
        id=id,
        guild_permissions=discord.Permissions(manage_guild=manage_guild),
        roles=[SimpleNamespace(id=r) for r in roles],
    )


def settings(**kw):
    return EffectiveSettings(**{"guild_id": 1, "max_repos": 25, "max_subscriptions": 100, **kw})


# --------------------------------------------------------------------------- roles


def test_manager_by_permission_or_role():
    assert is_manager(member(manage_guild=True), settings())
    assert not is_manager(member(roles=[MANAGER_ROLE]), settings())  # no manager role set
    assert is_manager(member(roles=[MANAGER_ROLE]), settings(manager_role_id=MANAGER_ROLE))
    assert not is_manager(member(roles=[123]), settings(manager_role_id=MANAGER_ROLE))


def test_administrator_counts_as_manager():
    # Member.guild_permissions resolves Administrator to every permission, Manage Server included.
    admin = member()
    admin.guild_permissions = discord.Permissions.all()
    assert is_manager(admin, settings())


def test_subscribe_access():
    open_server = settings()
    restricted = settings(subscriber_role_id=SUBSCRIBER_ROLE, manager_role_id=MANAGER_ROLE)
    assert has_subscribe_access(member(), open_server)
    assert not has_subscribe_access(member(), restricted)
    assert has_subscribe_access(member(roles=[SUBSCRIBER_ROLE]), restricted)
    # Managers always have subscribe access, with or without the subscriber role.
    assert has_subscribe_access(member(roles=[MANAGER_ROLE]), restricted)
    assert has_subscribe_access(member(manage_guild=True), restricted)


def test_can_modify_creator_or_manager():
    s = settings()
    assert can_modify(7, member(id=7), s)
    assert not can_modify(7, member(id=8), s)
    assert can_modify(7, member(id=8, manage_guild=True), s)


def test_blocked_notice():
    s = settings(blocked=True, blocked_reason="spam")
    assert blocked_notice(s, None) == "RepoDrop has been disabled for this server: spam"
    assert blocked_notice(settings(blocked=True), "https://x.test").endswith(
        "server.\nQuestions? https://x.test"
    )


# --------------------------------------------------------------------------- caps


def test_cap_violation():
    s = settings(max_repos=2, max_subscriptions=3)
    assert cap_violation(GuildUsage(1, 1), s, repo_already_followed=False) is None
    # Repo cap: a new repo is blocked, another channel for a followed repo is not.
    assert "2 repos" in cap_violation(GuildUsage(2, 2), s, repo_already_followed=False)
    assert cap_violation(GuildUsage(2, 2), s, repo_already_followed=True) is None
    # Subscription cap applies either way.
    assert "3 subscriptions" in cap_violation(GuildUsage(2, 3), s, repo_already_followed=True)


def test_effective_settings_fall_back_to_config():
    config = Settings(discord_token="x", github_token="x", _env_file=None)
    assert EffectiveSettings.resolve(5, None, config) == EffectiveSettings(
        guild_id=5, max_repos=25, max_subscriptions=100
    )
    row = GuildSettings(
        guild_id=5,
        max_repos=None,
        max_subscriptions=7,
        commits_allowed=False,
        blocked=False,
        manager_role_id=MANAGER_ROLE,
        embed_style="compact",
        latest_access="everyone",
        latest_allow_public=True,
    )
    effective = EffectiveSettings.resolve(5, row, config)
    assert (effective.max_repos, effective.max_subscriptions) == (25, 7)
    assert effective.commits_allowed is False
    assert effective.manager_role_id == MANAGER_ROLE and effective.embed_style == "compact"


@pytest.mark.parametrize(("raw", "expected"), [("1, 2", [1, 2]), ("[3,4]", [3, 4]), ("5", [5])])
def test_owner_ids_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("OWNER_IDS", raw)
    assert Settings(discord_token="x", github_token="x", _env_file=None).owner_ids == expected


# --------------------------------------------------------------------------- blocked check


class FakeCache:
    def __init__(self, s):
        self.s = s

    async def get(self, guild_id):
        return self.s


async def test_blocked_server_gets_notice_on_every_command():
    config = Settings(
        discord_token="x", github_token="x", block_contact_url="https://x.test", _env_file=None
    )
    cog = SubscriptionsCog(SimpleNamespace(settings=config))
    interaction = SimpleNamespace(
        guild_id=1,
        client=SimpleNamespace(
            guild_settings=FakeCache(settings(blocked=True, blocked_reason="r"))
        ),
    )
    with pytest.raises(AccessDenied, match="disabled for this server: r"):
        await cog.interaction_check(interaction)

    interaction.client.guild_settings = FakeCache(settings())
    assert await cog.interaction_check(interaction) is True
