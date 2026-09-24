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
from repodrop.github.schemas import Release, Repository
from repodrop.poller.scheduler import Poller

GUILD, CHANNEL, USER = 111, 222, 333
TEN_MIN = timedelta(minutes=10)


def settings(**overrides) -> Settings:
    return Settings(discord_token="x", github_token="x", _env_file=None, **overrides)


async def make_sub(session, *, kinds=("release",), channel=CHANNEL, branches=(), prereleases=False):
    repo = await queries.upsert_repo(
        session, github_id=42, full_name="octo/widget", default_branch="main"
    )
    sub, _ = await queries.upsert_subscription(
        session,
        guild_id=GUILD,
        channel_id=channel,
        repo_id=repo.id,
        kinds=list(kinds),
        branches=list(branches),
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
            branches=[],
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
        _, dev_branch = await make_sub(s, channel=5, kinds=("commit",), branches=("dev",))

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


def gh_repo(*, full_name="octo/widget", default_branch="main", private=False) -> Repository:
    return Repository.model_validate(
        {
            "id": 42,
            "full_name": full_name,
            "private": private,
            "default_branch": default_branch,
            "html_url": f"https://github.com/{full_name}",
            "owner": {
                "login": "octo",
                "html_url": "https://github.com/octo",
                "avatar_url": "https://github.com/octo.png",
            },
        }
    )


class FakeGitHub:
    def __init__(self) -> None:
        self.rate_limit = RateLimit(remaining=5000)
        self.releases: list[Release] = []
        self.etag = '"1"'
        self.error: Exception | None = None
        self.seen_etags: list[str | None] = []
        self.repo: Repository | None = gh_repo()  # None = gone (404 by ID)
        self.metadata_etag = '"m1"'
        self.branches = {"main"}

    async def list_releases(self, full_name, *, etag=None):
        self.seen_etags.append(etag)
        if self.error:
            raise self.error
        if etag == self.etag:
            return Conditional(items=None, etag=etag)
        return Conditional(items=list(self.releases), etag=self.etag)

    async def list_commits(self, full_name, branch, *, etag=None):
        if self.error:
            raise self.error
        if branch not in self.branches:
            raise NotFoundError(branch)
        return Conditional(items=[], etag=None)

    async def get_repo_by_id(self, github_id):
        if self.repo is None:
            raise NotFoundError("gone")
        return self.repo

    async def repo_metadata(self, github_id, *, etag=None):
        if self.repo is None:
            raise NotFoundError("gone")
        if etag == self.metadata_etag:
            return Conditional(items=None, etag=etag)
        return Conditional(items=self.repo, etag=self.metadata_etag)

    async def branch_exists(self, full_name, branch):
        return branch in self.branches


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


async def due_watch(sessions, key: WatchKey, last_seen="x") -> None:
    async with sessions.begin() as s:
        await queries.ensure_watch(s, key, initial_interval=TEN_MIN)
        await s.execute(
            update(RepoWatch)
            .where(
                RepoWatch.repo_id == key.repo_id,
                RepoWatch.kind == key.kind,
                RepoWatch.branch == key.branch,
            )
            .values(last_seen_id=last_seen, next_poll_at=datetime.now(UTC) - timedelta(seconds=1))
        )


def is_stopped(watch: RepoWatch) -> bool:
    return watch.next_poll_at.replace(tzinfo=None) == datetime.max


async def test_poller_disables_subscriptions_when_repo_is_gone(sessions):
    gh = FakeGitHub()
    gh.error = NotFoundError("gone")
    gh.repo = None  # the lookup by ID confirms it
    wake = asyncio.Event()
    poller = Poller(settings(), sessions, gh, wake)
    async with sessions.begin() as s:
        repo, sub = await make_sub(s, kinds=("release", "commit"))
        _, other = await make_sub(s, channel=2)
    await due_watch(sessions, WatchKey(repo.id, "release", ""))
    await due_watch(sessions, WatchKey(repo.id, "commit", "main"))

    await poller.run_cycle()
    assert wake.is_set()
    async with sessions() as s:
        for sub_id in (sub.id, other.id):
            disabled = await s.get(Subscription, sub_id)
            assert not disabled.active
            assert disabled.disabled_reason == "the repo was deleted or made private"
            assert disabled.disabled_at is not None and disabled.notified_at is None
        for watch in (await s.scalars(select(RepoWatch))).all():
            assert is_stopped(watch)
            # Covered by the subscriptions' notices, so no separate branch notice.
            assert watch.notified_at is not None
        assert await queries.pending_branch_notices(s) == []
        assert len(await queries.pending_disable_notices(s)) == 2


async def test_poller_transient_404_on_existing_repo(sessions):
    gh = FakeGitHub()
    gh.error = NotFoundError("glitch")  # but the repo still exists by ID
    poller = Poller(settings(), sessions, gh, asyncio.Event())
    async with sessions.begin() as s:
        repo, sub = await make_sub(s)
    key = WatchKey(repo.id, "release", "")
    await due_watch(sessions, key, last_seen="")  # baselined, no releases yet

    await poller.run_cycle()
    async with sessions() as s:
        assert (await s.get(Subscription, sub.id)).active
        watch = await queries.get_watch(s, key)
        assert not is_stopped(watch)
        assert "exists" in watch.last_error and watch.last_polled_at is not None

    # The next successful poll clears the error.
    gh.error = None
    await due_watch(sessions, key, last_seen="")
    await poller.run_cycle()
    async with sessions() as s:
        assert (await queries.get_watch(s, key)).last_error is None


async def test_poller_stops_only_the_deleted_branch(sessions):
    gh = FakeGitHub()
    gh.branches = {"main"}  # "dev" was deleted
    poller = Poller(settings(), sessions, gh, asyncio.Event())
    async with sessions.begin() as s:
        repo, sub = await make_sub(s, kinds=("commit",), branches=("main", "dev"))
    dev, main = WatchKey(repo.id, "commit", "dev"), WatchKey(repo.id, "commit", "main")
    await due_watch(sessions, dev)
    await due_watch(sessions, main)

    await poller.run_cycle()
    async with sessions() as s:
        assert (await s.get(Subscription, sub.id)).active
        dev_watch = await queries.get_watch(s, dev)
        assert is_stopped(dev_watch) and dev_watch.last_error == "branch `dev` no longer exists"
        assert not is_stopped(await queries.get_watch(s, main))
        [notice] = await queries.pending_branch_notices(s)
        assert (notice.key, notice.guild_id, notice.channel_id) == (dev, GUILD, CHANNEL)

    # Re-subscribing to the branch restarts the watch with a fresh baseline.
    async with sessions.begin() as s:
        await queries.ensure_watch(s, dev, initial_interval=TEN_MIN)
    async with sessions() as s:
        restarted = await queries.get_watch(s, dev)
        assert not is_stopped(restarted)
        assert restarted.last_seen_id is None and restarted.last_error is None


async def test_metadata_refresh_follows_default_branch_change(sessions):
    gh = FakeGitHub()
    poller = Poller(settings(), sessions, gh, asyncio.Event())
    async with sessions.begin() as s:
        repo, _ = await make_sub(s, kinds=("commit",))  # follows the default branch
        await make_sub(s, channel=2, kinds=("commit",), branches=("main",))  # lists main
        await queries.ensure_watch(s, WatchKey(repo.id, "commit", "main"), initial_interval=TEN_MIN)

    # Unchanged metadata: just recorded as checked.
    assert await poller.refresh_metadata() == 1
    assert await poller.refresh_metadata() == 0  # not due again for a day
    async with sessions() as s:
        assert (await s.get(Repo, repo.id)).metadata_etag == '"m1"'

    # The default branch moves to trunk, and the repo is renamed.
    gh.repo = gh_repo(full_name="octo/gadget", default_branch="trunk")
    gh.metadata_etag = '"m2"'
    async with sessions.begin() as s:
        await s.execute(update(Repo).values(metadata_checked_at=None))
    await poller.refresh_metadata()
    async with sessions() as s:
        fresh = await s.get(Repo, repo.id)
        assert (fresh.full_name, fresh.default_branch) == ("octo/gadget", "trunk")
        branches = set(await s.scalars(select(RepoWatch.branch)))
        # trunk for the default follower; main kept because a subscription lists it.
        assert branches == {"main", "trunk"}
        trunk = await queries.get_watch(s, WatchKey(repo.id, "commit", "trunk"))
        assert trunk.last_seen_id is None  # baselined on its first poll, so no history flood


async def test_metadata_refresh_disables_missing_repo(sessions):
    gh = FakeGitHub()
    gh.repo = None
    poller = Poller(settings(), sessions, gh, asyncio.Event())
    async with sessions.begin() as s:
        _, sub = await make_sub(s)
    await poller.refresh_metadata()
    async with sessions() as s:
        assert not (await s.get(Subscription, sub.id)).active


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
    def __init__(self, channel, guild=None) -> None:
        self.channel = channel
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild

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
            branches=[],
            include_prereleases=False,
            created_by=USER,
        )
        await queries.deactivate_subscription(s, first.id, "test")  # disabled still counts

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


# --------------------------------------------------------------------------- phase 2: branches


async def fan_out_to(s, repo, kind, *, branch="", external_id=None, prerelease=False, payload=None):
    event_id = await queries.insert_event(
        s,
        repo_id=repo.id,
        kind=kind,
        external_id=external_id or f"{kind}:{branch}",
        payload=payload or {},
    )
    await queries.fan_out_event(
        s,
        event_id=event_id,
        key=WatchKey(repo.id, kind, branch),
        default_branch="main",
        prerelease=prerelease,
    )
    rows = await s.scalars(select(Delivery.subscription_id).where(Delivery.event_id == event_id))
    return set(rows)


async def test_fan_out_with_branch_lists(sessions):
    async with sessions.begin() as s:
        repo, follows_default = await make_sub(s, channel=1, kinds=("commit",))
        _, lists_main = await make_sub(s, channel=2, kinds=("commit",), branches=("main",))
        _, dev_and_main = await make_sub(s, channel=3, kinds=("commit",), branches=("dev", "main"))
        _, dev_only = await make_sub(s, channel=4, kinds=("commit",), branches=("dev",))

        assert await fan_out_to(s, repo, "commit", branch="main") == {
            follows_default.id,
            lists_main.id,
            dev_and_main.id,
        }
        assert await fan_out_to(s, repo, "commit", branch="dev") == {dev_and_main.id, dev_only.id}
        assert await fan_out_to(s, repo, "commit", branch="other") == set()


async def test_fan_out_skips_blocked_servers_and_disallowed_commits(sessions):
    async with sessions.begin() as s:
        repo, releases = await make_sub(s, channel=1, kinds=("release",))
        _, commits = await make_sub(s, channel=2, kinds=("commit",))

        await queries.upsert_guild_settings(s, GUILD, commits_allowed=False)
        assert await fan_out_to(s, repo, "release", external_id="r1") == {releases.id}
        assert await fan_out_to(s, repo, "commit", branch="main", external_id="c1") == set()

        await queries.upsert_guild_settings(s, GUILD, commits_allowed=True, blocked=True)
        assert await fan_out_to(s, repo, "release", external_id="r2") == set()

        # Subscriptions are kept, so lifting the block resumes delivery.
        await queries.upsert_guild_settings(s, GUILD, blocked=False)
        assert await fan_out_to(s, repo, "commit", branch="main", external_id="c2") == {commits.id}


async def test_prune_keeps_watches_any_branch_list_needs(sessions):
    async with sessions.begin() as s:
        repo, _ = await make_sub(s, channel=1, kinds=("commit",))  # follows main (default)
        await make_sub(s, channel=2, kinds=("commit",), branches=("dev",))
        for branch in ("main", "dev", "stale"):
            await queries.ensure_watch(
                s, WatchKey(repo.id, "commit", branch), initial_interval=TEN_MIN
            )
        assert await queries.prune_orphans(s) == (1, 0)
        remaining = await s.scalars(select(RepoWatch.branch).order_by(RepoWatch.branch))
        assert list(remaining) == ["dev", "main"]


async def test_skip_removed_deliveries(sessions):
    async with sessions.begin() as s:
        repo, sub = await make_sub(
            s, kinds=("release", "tag", "commit"), branches=("main", "dev"), prereleases=True
        )
        await fan_out_to(s, repo, "tag", external_id="v1")
        await fan_out_to(s, repo, "commit", branch="dev", payload={"branch": "dev"})
        await fan_out_to(s, repo, "commit", branch="main", payload={"branch": "main"})
        await fan_out_to(
            s, repo, "release", external_id="pre", prerelease=True, payload={"prerelease": True}
        )
        await fan_out_to(s, repo, "release", external_id="stable", payload={"prerelease": False})

        skipped = await queries.skip_removed_deliveries(
            s, sub.id, kinds=["tag"], commit_branches=["dev"], prereleases=True
        )
        assert skipped == 3

        rows = await s.execute(
            select(Event.external_id, Delivery.status)
            .join(Event, Event.id == Delivery.event_id)
            .order_by(Event.external_id)
        )
        assert dict(rows.tuples().all()) == {
            "commit:dev": "skipped",
            "commit:main": "pending",
            "pre": "skipped",
            "stable": "pending",
            "v1": "skipped",
        }


async def test_upsert_reactivates_and_clears_disable(sessions):
    async with sessions.begin() as s:
        repo, sub = await make_sub(s)
        sub.active = False
        sub.disabled_reason = "lost access"
        sub.disabled_at = datetime.now(UTC)
        await s.flush()
        again, created = await queries.upsert_subscription(
            s,
            guild_id=GUILD,
            channel_id=CHANNEL,
            repo_id=repo.id,
            kinds=["release"],
            branches=[],
            include_prereleases=False,
            created_by=USER,
        )
        assert not created and again.active
        assert again.disabled_reason is None and again.disabled_at is None


# --------------------------------------------------------------------------- phase 3: health


async def test_dispatcher_records_disable_reason(sessions):
    sub = await seed_release_delivery(sessions)
    channel = FakeChannel(error=http_error(discord.NotFound, 404))
    await Dispatcher(settings(), sessions, FakeClient(channel), asyncio.Event()).drain()
    async with sessions() as s:
        disabled = await s.get(Subscription, sub.id)
        assert not disabled.active
        assert disabled.disabled_reason == "the channel was deleted"
        assert disabled.disabled_at is not None and disabled.notified_at is None


class FakeSystemChannel:
    def __init__(self, *, can_post=True):
        self.sent: list[str] = []
        self.can_post = can_post

    def permissions_for(self, member):
        return discord.Permissions(view_channel=self.can_post, send_messages=self.can_post)

    async def send(self, content, **kwargs):
        assert kwargs["allowed_mentions"].everyone is False
        self.sent.append(content)


async def test_admin_notices_grouped_per_server_and_sent_once(sessions):
    async with sessions.begin() as s:
        repo, gone = await make_sub(s, channel=1)
        _, lost = await make_sub(s, channel=2)
        _, branchy = await make_sub(s, channel=3, kinds=("commit",), branches=("dev",))
        await queries.deactivate_subscription(s, gone.id, "the repo was deleted or made private")
        await queries.deactivate_subscription(
            s, lost.id, "I lost permission to post in the channel"
        )
        dev = WatchKey(repo.id, "commit", "dev")
        await queries.ensure_watch(s, dev, initial_interval=TEN_MIN)
        await queries.stop_watch(s, dev, "branch `dev` no longer exists")

    system = FakeSystemChannel()
    guild = SimpleNamespace(system_channel=system, me=object())
    dispatcher = Dispatcher(settings(), sessions, FakeClient(None, guild), asyncio.Event())

    assert await dispatcher.send_notices() == 1  # one server
    [message] = system.sent
    assert "**octo/widget** in <#1>: the repo was deleted or made private." in message
    assert "**octo/widget** in <#2>: I lost permission to post in the channel." in message
    assert "**octo/widget**: branch `dev` no longer exists, so its commits in <#3>" in message

    # Already notified: nothing more is sent.
    assert await dispatcher.send_notices() == 0
    assert len(system.sent) == 1
    assert branchy.id  # still active, only the branch stopped


async def test_admin_notice_without_usable_system_channel_is_marked_anyway(sessions):
    async with sessions.begin() as s:
        _, sub = await make_sub(s)
        await queries.deactivate_subscription(s, sub.id, "the channel was deleted")
    system = FakeSystemChannel(can_post=False)
    guild = SimpleNamespace(system_channel=system, me=object())
    dispatcher = Dispatcher(settings(), sessions, FakeClient(None, guild), asyncio.Event())
    await dispatcher.send_notices()
    assert system.sent == []
    async with sessions() as s:
        assert (await s.get(Subscription, sub.id)).notified_at is not None


async def test_status_entries_from_health_query(sessions):
    from repodrop.bot.status import build_entries, paginate, summary

    async with sessions.begin() as s:
        repo, _ = await make_sub(s, channel=1, kinds=("release", "commit"), branches=("dev",))
        await make_sub(s, channel=2)
        _, disabled = await make_sub(s, channel=3)
        await queries.deactivate_subscription(s, disabled.id, "the channel was deleted")
        rel, dev = WatchKey(repo.id, "release", ""), WatchKey(repo.id, "commit", "dev")
        for key in (rel, dev):
            await queries.ensure_watch(s, key, initial_interval=TEN_MIN)
        await queries.update_watch(
            s,
            rel,
            etag=None,
            last_seen_id="",
            poll_interval=TEN_MIN,
            next_poll_at=datetime.now(UTC) + TEN_MIN,
        )
        await queries.stop_watch(s, dev, "branch `dev` no longer exists")

    async with sessions() as s:
        rows, watches = await queries.subscription_health(s, GUILD)
    entries = build_entries(
        rows, watches, lambda channel_id: ["I'm missing Embed Links"] if channel_id == 2 else []
    )
    by_channel = {e.channel_id: e for e in entries}
    assert by_channel[1].problems == ["Stopped: branch `dev` no longer exists"]
    assert by_channel[1].what == "releases, commits on `dev`"
    assert by_channel[1].last_polled_at is not None
    assert by_channel[2].problems == ["I'm missing Embed Links"]
    assert not by_channel[3].active and by_channel[3].problems == []  # disabled: no live checks
    assert summary(entries) == "0 healthy · 3 need attention"
    [page] = paginate(entries)
    assert "Disabled: the channel was deleted" in page
