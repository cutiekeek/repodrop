"""Queries, poller, and announcer against a real Postgres (see conftest `sessions`)."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
from sqlalchemy import select, text, update

from repodrop.announcer.dispatcher import Dispatcher
from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.models import Delivery, Event, Repo, RepoWatch, Subscription
from repodrop.db.queries import WatchKey
from repodrop.github.client import Conditional, NotFoundError, RateLimit
from repodrop.github.schemas import Release
from repodrop.poller.scheduler import Poller

GUILD, CHANNEL, USER = 111, 222, 333
TEN_MIN = timedelta(minutes=10)


def settings(**overrides) -> Settings:
    return Settings(discord_token="x", github_token="x", _env_file=None, **overrides)


async def make_sub(session, *, kinds=("release",), channel=CHANNEL, branch=None, prereleases=False):
    repo = await queries.upsert_repo(
        session, github_id=42, full_name="octo/widget", default_branch="main"
    )
    sub, _ = await queries.upsert_subscription(
        session,
        guild_id=GUILD,
        channel_id=channel,
        repo_id=repo.id,
        kinds=list(kinds),
        branch=branch,
        include_prereleases=prereleases,
        created_by=USER,
    )
    return repo, sub


async def make_due(session, key: WatchKey) -> None:
    await session.execute(
        update(RepoWatch)
        .where(RepoWatch.repo_id == key.repo_id, RepoWatch.kind == key.kind)
        .values(next_poll_at=datetime.now(UTC) - timedelta(seconds=1))
    )


# --------------------------------------------------------------------------- subscriptions


async def test_upserts_are_idempotent(sessions):
    async with sessions.begin() as s:
        repo, sub = await make_sub(s)
        renamed = await queries.upsert_repo(
            s, github_id=42, full_name="octo/gadget", default_branch="main"
        )
        assert renamed.id == repo.id and renamed.full_name == "octo/gadget"

        again, created = await queries.upsert_subscription(
            s,
            guild_id=GUILD,
            channel_id=CHANNEL,
            repo_id=repo.id,
            kinds=["release", "tag"],
            branch=None,
            include_prereleases=True,
            created_by=USER,
        )
        assert not created and again.id == sub.id
        assert await queries.guild_usage(s, GUILD) == queries.GuildUsage(repos=1, subscriptions=1)

    async with sessions() as s:
        [(listed, listed_repo)] = await queries.list_subscriptions(s, GUILD)
        assert listed.kinds == ["release", "tag"] and listed.include_prereleases
        assert listed_repo.full_name == "octo/gadget"
        found = await queries.find_subscription_by_repo_name(
            s, guild_id=GUILD, channel_id=CHANNEL, full_name="OCTO/Gadget"
        )
        assert found is not None
        assert await queries.search_guild_repo_names(s, GUILD, "gad") == ["octo/gadget"]
        assert await queries.search_guild_repo_names(s, GUILD, "%") == []  # LIKE wildcards escaped


# --------------------------------------------------------------------------- watches


async def test_claim_due_watches_leases(sessions):
    async with sessions.begin() as s:
        repo, _ = await make_sub(s)
        key = WatchKey(repo.id, "release", "")
        await queries.ensure_watch(s, key, initial_interval=TEN_MIN)
        await queries.ensure_watch(s, key, initial_interval=TEN_MIN)  # no-op
        # Newly created watches wait for the subscribe flow to baseline them.
        assert await queries.claim_due_watches(s, limit=10, lease=TEN_MIN) == []
        await make_due(s, key)

    async with sessions.begin() as s:
        assert await queries.oldest_due_poll_lag(s) > timedelta(0)
        [claimed] = await queries.claim_due_watches(s, limit=10, lease=TEN_MIN)
        assert claimed.key == key and claimed.last_seen_id is None
        assert claimed.poll_interval == TEN_MIN

    async with sessions.begin() as s:
        # Leased: not claimable again until the lease runs out.
        assert await queries.claim_due_watches(s, limit=10, lease=TEN_MIN) == []


async def test_concurrent_claims_do_not_overlap(sessions):
    async with sessions.begin() as s:
        repo, _ = await make_sub(s, kinds=("release", "tag"))
        for kind in ("release", "tag"):
            key = WatchKey(repo.id, kind, "")
            await queries.ensure_watch(s, key, initial_interval=TEN_MIN)
            await make_due(s, key)

    async def claim():
        async with sessions.begin() as s:
            return await queries.claim_due_watches(s, limit=1, lease=TEN_MIN)

    a, b = await asyncio.gather(claim(), claim())
    assert {w.key.kind for w in a + b} == {"release", "tag"}


# --------------------------------------------------------------------------- events and fan-out


async def test_fan_out_filters_by_kind_prerelease_and_branch(sessions):
    async with sessions.begin() as s:
        repo, rel_only = await make_sub(s, channel=1, kinds=("release",))
        _, with_pre = await make_sub(s, channel=2, kinds=("release",), prereleases=True)
        _, tags_only = await make_sub(s, channel=3, kinds=("tag",))
        _, default_branch = await make_sub(s, channel=4, kinds=("commit",))
        _, dev_branch = await make_sub(s, channel=5, kinds=("commit",), branch="dev")

        async def fan_out(kind, external_id, *, branch="", prerelease=False):
            event_id = await queries.insert_event(
                s, repo_id=repo.id, kind=kind, external_id=external_id, payload={}
            )
            assert event_id is not None
            await queries.fan_out_event(
                s,
                event_id=event_id,
                key=WatchKey(repo.id, kind, branch),
                default_branch="main",
                prerelease=prerelease,
            )
            rows = await s.scalars(
                select(Delivery.subscription_id).where(Delivery.event_id == event_id)
            )
            return set(rows)

        assert await fan_out("release", "1") == {rel_only.id, with_pre.id}
        assert await fan_out("release", "2", prerelease=True) == {with_pre.id}
        assert await fan_out("tag", "v1") == {tags_only.id}
        assert await fan_out("commit", "main:a..b", branch="main") == {default_branch.id}
        assert await fan_out("commit", "dev:a..b", branch="dev") == {dev_branch.id}

        # Same event again is a no-op.
        assert (
            await queries.insert_event(
                s, repo_id=repo.id, kind="release", external_id="1", payload={}
            )
            is None
        )


# --------------------------------------------------------------------------- deliveries


async def seed_delivery(sessions, *, kinds=("release",)):
    async with sessions.begin() as s:
        repo, sub = await make_sub(s, kinds=kinds)
        event_id = await queries.insert_event(
            s, repo_id=repo.id, kind="release", external_id="1", payload={"hello": "world"}
        )
        await queries.fan_out_event(
            s, event_id=event_id, key=WatchKey(repo.id, "release", ""), default_branch="main"
        )
    return repo, sub


async def test_claim_deliveries_counts_attempts_and_leases(sessions):
    _, sub = await seed_delivery(sessions)
    async with sessions.begin() as s:
        [d] = await queries.claim_deliveries(s, limit=10, lease=TEN_MIN)
        assert d.attempts == 1
        assert (d.subscription_id, d.guild_id, d.channel_id) == (sub.id, GUILD, CHANNEL)
        assert d.event_kind == "release" and d.payload == {"hello": "world"}
        assert await queries.pending_delivery_count(s) == 1
    async with sessions.begin() as s:
        assert await queries.claim_deliveries(s, limit=10, lease=TEN_MIN) == []
        # A retry with zero delay makes it claimable again, with the attempt counted.
        await queries.schedule_delivery_retry(s, d.id, error="boom", delay=timedelta(0))
    async with sessions.begin() as s:
        [d2] = await queries.claim_deliveries(s, limit=10, lease=TEN_MIN)
        assert d2.attempts == 2
        await queries.mark_delivery_sent(s, d2.id, message_id=999)
    async with sessions() as s:
        assert await queries.pending_delivery_count(s) == 0
        delivery = await s.get(Delivery, d2.id)
        assert delivery.status == "sent" and delivery.message_id == 999


# --------------------------------------------------------------------------- cleanup


async def test_prune_orphans_and_guild_removal(sessions):
    repo, _ = await seed_delivery(sessions, kinds=("release", "commit"))
    async with sessions.begin() as s:
        for key in (WatchKey(repo.id, "release", ""), WatchKey(repo.id, "commit", "main")):
            await queries.ensure_watch(s, key, initial_interval=TEN_MIN)
        await queries.ensure_watch(s, WatchKey(repo.id, "tag", ""), initial_interval=TEN_MIN)
        await queries.ensure_watch(s, WatchKey(repo.id, "commit", "dev"), initial_interval=TEN_MIN)
        # tag and commit@dev aren't wanted by any subscription.
        assert await queries.prune_orphans(s) == (2, 0)

    async with sessions.begin() as s:
        assert await queries.delete_guild_subscriptions(s, GUILD) == 1
        assert await queries.prune_orphans(s) == (2, 1)

    async with sessions() as s:
        for model in (Repo, RepoWatch, Subscription, Event, Delivery):
            assert (await s.scalars(select(model))).all() == [], model.__name__


# --------------------------------------------------------------------------- poller


def gh_release(id: int, published: datetime, *, prerelease=False) -> Release:
    return Release.model_validate(
        {
            "id": id,
            "tag_name": f"v{id}",
            "draft": False,
            "prerelease": prerelease,
            "html_url": f"https://github.com/octo/widget/releases/tag/v{id}",
            "created_at": published.isoformat(),
            "published_at": published.isoformat(),
        }
    )


class FakeGitHub:
    def __init__(self) -> None:
        self.rate_limit = RateLimit(remaining=5000)
        self.releases: list[Release] = []
        self.etag = '"1"'
        self.error: Exception | None = None
        self.seen_etags: list[str | None] = []

    async def list_releases(self, full_name, *, etag=None):
        self.seen_etags.append(etag)
        if self.error:
            raise self.error
        if etag == self.etag:
            return Conditional(items=None, etag=etag)
        return Conditional(items=list(self.releases), etag=self.etag)


async def test_poller_baseline_detect_and_not_modified(sessions):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    gh = FakeGitHub()
    gh.releases = [gh_release(1, t0)]
    wake = asyncio.Event()
    poller = Poller(settings(), sessions, gh, wake)

    async with sessions.begin() as s:
        repo, sub = await make_sub(s)
        key = WatchKey(repo.id, "release", "")
        await queries.ensure_watch(s, key, initial_interval=TEN_MIN)

    # Subscribe-time baseline: remembers v1, announces nothing.
    await poller.baseline(key, "octo/widget")
    async with sessions() as s:
        watch = await queries.get_watch(s, key)
        assert watch.last_seen_id == t0.isoformat() and watch.etag == '"1"'

    # Unchanged (304): nothing announced; quiet repo backs off.
    async with sessions.begin() as s:
        await make_due(s, key)
    assert await poller.run_cycle() == 1
    assert gh.seen_etags[-1] == '"1"'
    assert not wake.is_set()
    async with sessions() as s:
        assert (await queries.get_watch(s, key)).poll_interval == timedelta(minutes=15)

    # Two new releases (one a prerelease the subscription didn't opt into).
    gh.releases = [
        gh_release(3, t0 + timedelta(hours=2), prerelease=True),
        gh_release(2, t0 + timedelta(hours=1)),
        gh_release(1, t0),
    ]
    gh.etag = '"2"'
    async with sessions.begin() as s:
        await make_due(s, key)
    await poller.run_cycle()
    assert wake.is_set()
    async with sessions() as s:
        events = (await s.scalars(select(Event).order_by(Event.id))).all()
        assert [e.external_id for e in events] == ["2", "3"]
        deliveries = (await s.scalars(select(Delivery))).all()
        assert [(d.subscription_id, d.event_id) for d in deliveries] == [(sub.id, events[0].id)]
        watch = await queries.get_watch(s, key)
        assert watch.last_seen_id == (t0 + timedelta(hours=2)).isoformat()
        assert watch.etag == '"2"'
        assert watch.poll_interval == timedelta(minutes=5)  # changed: poll faster


async def test_poller_backs_off_on_404(sessions):
    gh = FakeGitHub()
    gh.error = NotFoundError("gone")
    poller = Poller(settings(), sessions, gh, asyncio.Event())
    async with sessions.begin() as s:
        repo, _ = await make_sub(s)
        key = WatchKey(repo.id, "release", "")
        await queries.ensure_watch(s, key, initial_interval=TEN_MIN)
        await make_due(s, key)
    await poller.run_cycle()
    async with sessions() as s:
        watch = await queries.get_watch(s, key)
        assert watch.next_poll_at > datetime.now(UTC) + timedelta(minutes=50)


# --------------------------------------------------------------------------- announcer


class FakeChannel(discord.abc.Messageable):
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[dict] = []

    async def _get_channel(self):
        return self

    async def send(self, **kwargs):
        if self.error:
            raise self.error
        self.sent.append(kwargs)
        return SimpleNamespace(id=12345)


class FakeClient:
    def __init__(self, channel) -> None:
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel

    async def fetch_channel(self, channel_id):
        raise AssertionError("should use the cache")


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "nope")


async def seed_release_delivery(sessions):
    async with sessions.begin() as s:
        repo, sub = await make_sub(s)
        payload = {
            "repo": {"full_name": "octo/widget", "html_url": "https://github.com/octo/widget"},
            "id": 1,
            "tag_name": "v1",
            "name": "v1",
            "body": "@everyone notes",
            "html_url": "https://github.com/octo/widget/releases/tag/v1",
            "prerelease": False,
            "published_at": "2026-01-01T00:00:00+00:00",
            "author": None,
        }
        event_id = await queries.insert_event(
            s, repo_id=repo.id, kind="release", external_id="1", payload=payload
        )
        await queries.fan_out_event(
            s, event_id=event_id, key=WatchKey(repo.id, "release", ""), default_branch="main"
        )
    return sub


async def test_dispatcher_sends_without_mentions(sessions):
    await seed_release_delivery(sessions)
    channel = FakeChannel()
    await Dispatcher(settings(), sessions, FakeClient(channel), asyncio.Event()).drain()

    [sent] = channel.sent
    assert sent["embed"].title == "v1"
    assert sent["allowed_mentions"].everyone is False
    async with sessions() as s:
        [d] = (await s.scalars(select(Delivery))).all()
        assert d.status == "sent" and d.message_id == 12345


async def test_dispatcher_deactivates_on_forbidden(sessions):
    sub = await seed_release_delivery(sessions)
    channel = FakeChannel(error=http_error(discord.Forbidden, 403))
    await Dispatcher(settings(), sessions, FakeClient(channel), asyncio.Event()).drain()
    async with sessions() as s:
        [d] = (await s.scalars(select(Delivery))).all()
        assert d.status == "skipped" and d.last_error.startswith("forbidden")
        assert (await s.get(Subscription, sub.id)).active is False


async def test_dispatcher_retries_then_fails(sessions):
    await seed_release_delivery(sessions)
    channel = FakeChannel(error=http_error(discord.DiscordServerError, 503))
    dispatcher = Dispatcher(
        settings(delivery_max_attempts=2), sessions, FakeClient(channel), asyncio.Event()
    )

    await dispatcher.drain()
    async with sessions.begin() as s:
        [d] = (await s.scalars(select(Delivery))).all()
        assert d.status == "pending" and d.attempts == 1
        assert d.next_attempt_at > datetime.now(UTC)
        await s.execute(text("UPDATE deliveries SET next_attempt_at = now()"))

    await dispatcher.drain()
    async with sessions() as s:
        [d] = (await s.scalars(select(Delivery))).all()
        assert d.status == "failed" and d.attempts == 2 and "503" in d.last_error


# --------------------------------------------------------------------------- guild settings


async def test_guild_usage_counts_distinct_repos_and_disabled(sessions):
    async with sessions.begin() as s:
        repo, first = await make_sub(s, channel=1)
        await make_sub(s, channel=2)  # same repo, another channel
        other = await queries.upsert_repo(
            s, github_id=43, full_name="octo/other", default_branch="main"
        )
        await queries.upsert_subscription(
            s,
            guild_id=GUILD,
            channel_id=1,
            repo_id=other.id,
            kinds=["release"],
            branch=None,
            include_prereleases=False,
            created_by=USER,
        )
        await queries.deactivate_subscription(s, first.id)  # disabled still counts

        assert await queries.guild_usage(s, GUILD) == queries.GuildUsage(repos=2, subscriptions=3)
        assert await queries.guild_usage(s, 999) == queries.GuildUsage(repos=0, subscriptions=0)
        assert await queries.guild_follows_repo(s, GUILD, repo.id)
        assert not await queries.guild_follows_repo(s, 999, repo.id)


async def test_search_repo_names_by_creator(sessions):
    async with sessions.begin() as s:
        await make_sub(s)  # created by USER
        assert await queries.search_guild_repo_names(s, GUILD, "", created_by=USER) == [
            "octo/widget"
        ]
        assert await queries.search_guild_repo_names(s, GUILD, "", created_by=USER + 1) == []


async def test_settings_cache_defaults_updates_and_invalidation(sessions):
    from repodrop.guild_settings import GuildSettingsCache

    cache = GuildSettingsCache(settings(), sessions)
    defaults = await cache.get(GUILD)
    assert (defaults.max_repos, defaults.max_subscriptions, defaults.blocked) == (25, 100, False)

    updated = await cache.update(GUILD, max_repos=3, manager_role_id=77)
    assert (updated.max_repos, updated.max_subscriptions, updated.manager_role_id) == (3, 100, 77)

    # A direct database edit is only seen after invalidation (or the TTL).
    async with sessions.begin() as s:
        await queries.upsert_guild_settings(s, GUILD, blocked=True, blocked_reason="spam")
    assert (await cache.get(GUILD)).blocked is False
    cache.invalidate(GUILD)
    assert (await cache.get(GUILD)).blocked_reason == "spam"


async def test_guild_removal_keeps_settings_only_when_blocked(sessions):
    async with sessions.begin() as s:
        await queries.upsert_guild_settings(s, 1, manager_role_id=5)
        await queries.upsert_guild_settings(s, 2, blocked=True, blocked_reason="abuse")
        await queries.delete_guild_settings_unless_blocked(s, 1)
        await queries.delete_guild_settings_unless_blocked(s, 2)
        assert await queries.get_guild_settings(s, 1) is None
        assert (await queries.get_guild_settings(s, 2)).blocked_reason == "abuse"


async def test_guild_lock_serializes_transactions(sessions):
    order: list[str] = []
    first_has_lock = asyncio.Event()

    async def first():
        async with sessions.begin() as s:
            await queries.lock_guild(s, GUILD)
            first_has_lock.set()
            await asyncio.sleep(0.3)
            order.append("first done")

    async def second():
        await first_has_lock.wait()
        async with sessions.begin() as s:
            await queries.lock_guild(s, GUILD)  # blocks until first commits
            order.append("second locked")

    async def other_guild():
        await first_has_lock.wait()
        async with sessions.begin() as s:
            await queries.lock_guild(s, GUILD + 1)  # different server: no wait
            order.append("other guild locked")

    await asyncio.gather(first(), second(), other_guild())
    assert order == ["other guild locked", "first done", "second locked"]
