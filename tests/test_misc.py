from datetime import UTC, datetime, timedelta

import pytest

from repodrop.announcer.dispatcher import retry_delay
from repodrop.announcer.embeds import DESCRIPTION_LIMIT, TITLE_LIMIT, commit_embed, release_embed
from repodrop.config import DatabaseSettings
from repodrop.github.client import RateLimit
from repodrop.github.names import parse_repo
from repodrop.poller.scheduler import next_interval, schedule_at

REPO = {
    "full_name": "octo/widget",
    "html_url": "https://github.com/octo/widget",
    "owner_avatar_url": "https://github.com/octo.png",
}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("octo/widget", "octo/widget"),
        (" octo/widget ", "octo/widget"),
        ("https://github.com/octo/widget", "octo/widget"),
        ("github.com/octo/widget.git", "octo/widget"),
        ("https://www.github.com/octo/widget/releases/tag/v1?x=1", "octo/widget"),
        ("octo/my.repo_name-2", "octo/my.repo_name-2"),
        ("octo", None),
        ("octo/widget/extra", None),
        ("https://gitlab.com/octo/widget", None),
        ("-bad/widget", None),
        ("octo/..", None),
    ],
)
def test_parse_repo(value, expected):
    assert parse_repo(value) == expected


def test_database_url_normalized():
    assert (
        DatabaseSettings(database_url="postgres://u:p@h/db").database_url
        == "postgresql+asyncpg://u:p@h/db"
    )


def test_next_interval():
    lo, hi = timedelta(minutes=5), timedelta(minutes=60)
    assert (
        next_interval(timedelta(minutes=40), changed=True, min_interval=lo, max_interval=hi) == lo
    )
    assert next_interval(
        timedelta(minutes=10), changed=False, min_interval=lo, max_interval=hi
    ) == timedelta(minutes=15)
    assert (
        next_interval(timedelta(minutes=50), changed=False, min_interval=lo, max_interval=hi) == hi
    )


def test_schedule_at_waits_for_rate_limit_reset():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reset = now + timedelta(minutes=30)
    at = schedule_at(now, timedelta(minutes=5), RateLimit(remaining=10, reset_at=reset), floor=500)
    assert at > reset
    at = schedule_at(
        now, timedelta(minutes=5), RateLimit(remaining=4000, reset_at=reset), floor=500
    )
    assert at < reset


def test_retry_delay_backs_off_and_caps():
    assert retry_delay(1) == timedelta(seconds=30)
    assert retry_delay(2) == timedelta(seconds=60)
    assert retry_delay(20) == timedelta(hours=1)


def test_release_embed_truncates_long_notes():
    embed = release_embed(
        {
            "repo": REPO,
            "id": 1,
            "tag_name": "v1",
            "name": "x" * 500,
            "body": "y" * 6000,
            "body_truncated": True,
            "html_url": "https://github.com/octo/widget/releases/tag/v1",
            "prerelease": False,
            "published_at": "2026-01-01T00:00:00+00:00",
            "author": None,
        }
    )
    assert len(embed.title) <= TITLE_LIMIT
    assert len(embed.description) <= DESCRIPTION_LIMIT
    assert embed.description.endswith("(https://github.com/octo/widget/releases/tag/v1)")


def test_commit_embed_fits_limit():
    commits = [
        {
            "sha": f"{i:040x}",
            "message": "m" * 200,
            "html_url": f"https://github.com/octo/widget/commit/{i:040x}",
            "author_name": "Octo",
            "author": None,
        }
        for i in range(20)
    ]
    embed = commit_embed(
        {
            "repo": REPO,
            "branch": "main",
            "compare_url": "https://github.com/octo/widget/compare/a...b",
            "truncated": True,
            "commits": commits,
        }
    )
    assert len(embed.description) <= DESCRIPTION_LIMIT
    assert "more" in embed.description
    assert embed.title == "[octo/widget:main] 20+ new commits"


def test_durations_accept_seconds_and_iso(monkeypatch):
    from repodrop.config import Settings

    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("POLL_MIN_INTERVAL", "300")
    monkeypatch.setenv("POLL_MAX_INTERVAL", "PT2H")
    monkeypatch.setenv("DEV_GUILD_ID", "")
    s = Settings(_env_file=None)
    assert s.poll_min_interval == timedelta(minutes=5)
    assert s.poll_max_interval == timedelta(hours=2)
    assert s.dev_guild_id is None
