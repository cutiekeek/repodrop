"""Phase 4: Compare data, link buttons, embed styles, and /repodrop latest."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from repodrop.announcer.embeds import (
    Presentation,
    build_message,
    link_view,
    release_buttons,
    release_embed,
    tag_buttons,
)
from repodrop.bot.checks import AccessDenied
from repodrop.bot.cogs.subscriptions import SubscriptionsCog, UserError
from repodrop.config import Settings
from repodrop.github.client import Conditional
from repodrop.github.schemas import Release, Repository, Tag
from repodrop.guild_settings import EffectiveSettings
from repodrop.poller.detectors import (
    RepoRef,
    detect_releases,
    detect_tags,
    previous_release_tag,
    release_payload,
)

REPO = RepoRef("octo/widget")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def release(id, minutes, *, pre=False, assets=0) -> Release:
    return Release.model_validate(
        {
            "id": id,
            "tag_name": f"v{id}",
            "draft": False,
            "prerelease": pre,
            "html_url": f"https://github.com/octo/widget/releases/tag/v{id}",
            "created_at": T0.isoformat(),
            "published_at": (T0 + timedelta(minutes=minutes)).isoformat(),
            "assets": [
                {
                    "name": f"widget-{n}.zip",
                    "browser_download_url": f"https://github.com/octo/widget/releases/download/v{id}/widget-{n}.zip",
                    "size": 1536 * (n + 1),
                }
                for n in range(assets)
            ],
        }
    )


def tag(name) -> Tag:
    return Tag.model_validate({"name": name, "commit": {"sha": f"{name}-sha"}})


# --------------------------------------------------------------------------- payloads


def test_previous_release_tag_prefers_previous_stable_for_stable():
    v1, rc, v2 = release(1, 10), release(2, 20, pre=True), release(3, 30)
    releases = [v2, rc, v1]
    assert previous_release_tag(v2, releases) == "v1"  # skips the release candidate
    assert previous_release_tag(rc, releases) == "v1"
    assert previous_release_tag(v1, releases) is None


def test_release_payload_carries_previous_tag_and_no_assets():
    # GitHub returns assets, but announcements don't use them, so they aren't stored.
    d = detect_releases(
        REPO, [release(2, 20, assets=5), release(1, 10)], (T0 + timedelta(minutes=10)).isoformat()
    )
    [event] = d.events
    assert event.payload["previous_tag"] == "v1"
    assert not any("asset" in key for key in event.payload)


def test_tag_payload_previous_tag():
    d = detect_tags(REPO, [tag("v3"), tag("v2"), tag("v1")], "v1")
    assert [(e.external_id, e.payload["previous_tag"]) for e in d.events] == [
        ("v2", "v1"),
        ("v3", "v2"),
    ]


# --------------------------------------------------------------------------- buttons and styles


def test_release_buttons_are_view_and_compare_only():
    p = release_payload(REPO, release(2, 20, assets=4), previous_tag="v1")
    assert release_buttons(p) == [
        ("View release", "https://github.com/octo/widget/releases/tag/v2"),
        ("Compare", "https://github.com/octo/widget/compare/v1...v2"),
    ]
    # The first release has nothing to compare against.
    first = release_payload(REPO, release(1, 10))
    assert [label for label, _ in release_buttons(first)] == ["View release"]


def test_old_payloads_without_new_fields_still_render():
    p = release_payload(REPO, release(1, 10))
    del p["previous_tag"]
    assert [label for label, _ in release_buttons(p)] == ["View release"]
    assert release_embed(p).footer.text == "Release"  # name == tag, no author


def test_compact_style_drops_notes():
    p = release_payload(REPO, release(2, 20))
    p["body"] = "Lots of notes"
    assert release_embed(p).description == "Lots of notes"
    assert release_embed(p, compact=True).description is None


async def test_compact_and_full_have_the_same_buttons():
    p = release_payload(REPO, release(2, 20, assets=3), previous_tag="v1")
    for style in ("full", "compact"):
        _, view = build_message("release", p, Presentation(style))
        assert [b.label for b in view.children] == ["View release", "Compare"]


def test_tag_buttons_and_compare_url_encoding():
    p = {
        "repo": REPO.payload(),
        "name": "release/2.0+build",
        "sha": "abc",
        "html_url": "https://github.com/octo/widget/tree/release/2.0+build",
        "previous_tag": "release/1.0",
    }
    buttons = dict(tag_buttons(p))
    assert buttons["Compare"] == (
        "https://github.com/octo/widget/compare/release/1.0...release/2.0%2Bbuild"
    )


async def test_link_view_limits_and_filters():
    # discord.ui.View needs a running event loop, hence async.
    long_label = "x" * 200
    view = link_view(
        [(long_label, "https://a.test")]
        + [("bad", "javascript:alert(1)")]
        + [(f"b{i}", f"https://b{i}.test") for i in range(10)]
    )
    assert len(view.children) == 5
    assert all(item.style is discord.ButtonStyle.link for item in view.children)
    assert len(view.children[0].label) == 80
    assert all(item.url.startswith("https://") for item in view.children)
    assert link_view([]) is None


async def test_build_message_commit_has_view_commits_button():
    payload = {
        "repo": REPO.payload(),
        "branch": "main",
        "compare_url": "https://github.com/octo/widget/compare/a...b",
        "truncated": False,
        "commits": [],
    }
    _, view = build_message("commit", payload, Presentation())
    assert [(b.label, b.url) for b in view.children] == [
        ("View commits", "https://github.com/octo/widget/compare/a...b")
    ]


# --------------------------------------------------------------------------- /repodrop latest


def gh_repo() -> Repository:
    return Repository.model_validate(
        {
            "id": 42,
            "full_name": "Octo/Widget",
            "private": False,
            "default_branch": "main",
            "html_url": "https://github.com/Octo/Widget",
            "owner": {
                "login": "Octo",
                "html_url": "https://github.com/Octo",
                "avatar_url": "https://github.com/Octo.png",
            },
        }
    )


class FakeGitHub:
    def __init__(self, releases=(), tags=()):
        self.releases, self.tags = list(releases), list(tags)
        self.calls = 0

    async def get_repo(self, full_name):
        self.calls += 1
        return gh_repo()

    async def list_releases(self, full_name, *, etag=None):
        self.calls += 1
        return Conditional(items=self.releases, etag=None)

    async def list_tags(self, full_name, *, etag=None, per_page=10):
        self.calls += 1
        return Conditional(items=self.tags[:per_page], etag=None)


def cog_with(github) -> SubscriptionsCog:
    config = Settings(discord_token="x", github_token="x", _env_file=None)
    return SubscriptionsCog(SimpleNamespace(settings=config, github=github))


async def test_latest_prefers_newest_stable_release_and_caches():
    gh = FakeGitHub(releases=[release(3, 30, pre=True), release(2, 20), release(1, 10)])
    cog = cog_with(gh)
    kind, payload = await cog._latest_payload("octo/widget")
    assert kind == "release" and payload["tag_name"] == "v2"
    assert payload["previous_tag"] == "v1"
    assert payload["repo"]["full_name"] == "Octo/Widget"  # canonical name from GitHub

    calls = gh.calls
    assert await cog._latest_payload("https://github.com/OCTO/widget") == (kind, payload)
    assert gh.calls == calls  # served from the cache


async def test_latest_falls_back_to_newest_tag():
    cog = cog_with(FakeGitHub(releases=[release(1, 10, pre=True)], tags=[tag("v9"), tag("v8")]))
    kind, payload = await cog._latest_payload("octo/widget")
    assert kind == "tag" and payload["name"] == "v9" and payload["previous_tag"] == "v8"


async def test_latest_with_nothing_to_show():
    with pytest.raises(UserError, match="no releases or tags"):
        await cog_with(FakeGitHub())._latest_payload("octo/widget")


def interaction_for(settings: EffectiveSettings, *, manager: bool):
    member = SimpleNamespace(
        id=5, guild_permissions=discord.Permissions(manage_guild=manager), roles=[]
    )
    cache = SimpleNamespace(get=AsyncMock(return_value=settings))
    return SimpleNamespace(
        guild_id=1,
        user=member,
        client=SimpleNamespace(guild_settings=cache),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def settings(**kw) -> EffectiveSettings:
    return EffectiveSettings(**{"guild_id": 1, "max_repos": 25, "max_subscriptions": 100, **kw})


async def test_latest_managers_only_setting():
    cog = cog_with(FakeGitHub(releases=[release(1, 10)]))
    locked = settings(latest_access="managers")
    with pytest.raises(AccessDenied, match="Only managers"):
        await cog.latest.callback(cog, interaction_for(locked, manager=False), "octo/widget")
    interaction = interaction_for(locked, manager=True)
    await cog.latest.callback(cog, interaction, "octo/widget")
    interaction.followup.send.assert_awaited_once()


async def test_latest_public_turned_off_answers_privately_with_note():
    cog = cog_with(FakeGitHub(releases=[release(1, 10)]))
    interaction = interaction_for(settings(latest_allow_public=False), manager=False)
    await cog.latest.callback(cog, interaction, "octo/widget", public=True)
    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert "turned off" in kwargs["content"]
    assert [b.label for b in kwargs["view"].children] == ["View release"]

    public_ok = interaction_for(settings(), manager=False)
    await cog.latest.callback(cog, public_ok, "octo/widget", public=True)
    public_ok.response.defer.assert_awaited_once_with(ephemeral=False, thinking=True)
    assert public_ok.followup.send.await_args.kwargs["content"] is None
