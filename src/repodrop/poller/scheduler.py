"""Due-watch selection, polling, and adaptive intervals."""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import logfire

from repodrop import observability
from repodrop.config import Settings
from repodrop.db import queries
from repodrop.db.models import Kind, Repo
from repodrop.db.queries import ClaimedWatch, WatchKey
from repodrop.db.session import SessionFactory
from repodrop.github.client import (
    Conditional,
    GitHubClient,
    GitHubError,
    NotFoundError,
    RateLimit,
    RateLimitedError,
)
from repodrop.poller import detectors
from repodrop.poller.detectors import Detection, RepoRef

# A claimed watch is hidden from other claims for this long; a crashed poll retries after it.
CLAIM_LEASE = timedelta(minutes=5)


def next_interval(
    current: timedelta, *, changed: bool, min_interval: timedelta, max_interval: timedelta
) -> timedelta:
    """Poll recently-changed repos often; back off gradually on quiet ones."""
    if changed:
        return min_interval
    return min(max(current * 1.5, min_interval), max_interval)


def schedule_at(now: datetime, interval: timedelta, rate_limit: RateLimit, floor: int) -> datetime:
    # +/-10% jitter so watches created together don't stay in lockstep.
    at = now + interval * random.uniform(0.9, 1.1)
    if rate_limit.is_low(floor) and rate_limit.reset_at and rate_limit.reset_at > at:
        at = rate_limit.reset_at + timedelta(seconds=random.uniform(5, 60))
    return at


class Poller:
    def __init__(
        self,
        settings: Settings,
        sessions: SessionFactory,
        github: GitHubClient,
        wake_announcer: asyncio.Event,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._github = github
        self._wake_announcer = wake_announcer
        self._semaphore = asyncio.Semaphore(settings.poll_concurrency)

    # ------------------------------------------------------------------ loops

    async def run(self) -> None:
        while True:
            try:
                claimed = await self.run_cycle()
            except Exception:
                logfire.exception("poll cycle failed")
                claimed = 0
            # A full batch means there's probably more due work; go again immediately.
            if claimed < self._settings.poll_batch_size:
                await asyncio.sleep(self._settings.poll_tick.total_seconds())

    async def run_maintenance(self) -> None:
        while True:
            await asyncio.sleep(self._settings.maintenance_interval.total_seconds())
            try:
                with logfire.span("maintenance"):
                    async with self._sessions.begin() as session:
                        watches, repos = await queries.prune_orphans(session)
                    logfire.info(
                        "pruned {watches} orphan watches and {repos} orphan repos",
                        watches=watches,
                        repos=repos,
                    )
            except Exception:
                logfire.exception("maintenance failed")

    async def run_cycle(self) -> int:
        with logfire.span("poll_cycle") as span:
            async with self._sessions.begin() as session:
                lag = await queries.oldest_due_poll_lag(session)
                watches = await queries.claim_due_watches(
                    session, limit=self._settings.poll_batch_size, lease=CLAIM_LEASE
                )
                repos = await queries.get_repos(session, list({w.key.repo_id for w in watches}))
            observability.poll_lag.set(lag.total_seconds() if lag else 0)
            span.set_attribute("due", len(watches))
            if not watches:
                return 0

            results = await asyncio.gather(
                *(self._poll_guarded(w, repos[w.key.repo_id]) for w in watches)
            )
            new_deliveries = sum(results)
            span.set_attribute("new_deliveries", new_deliveries)
            if new_deliveries:
                self._wake_announcer.set()
            return len(watches)

    # ------------------------------------------------------------------ single watch

    async def _poll_guarded(self, watch: ClaimedWatch, repo: Repo) -> int:
        async with self._semaphore:
            try:
                return await self._poll(watch, repo)
            except Exception:
                logfire.exception(
                    "poll failed for {repo} {kind}", repo=repo.full_name, kind=watch.key.kind
                )
                return 0

    async def _poll(self, watch: ClaimedWatch, repo: Repo) -> int:
        key = watch.key
        with logfire.span(
            "poll_repo {repo} {kind}", repo=repo.full_name, kind=key.kind, branch=key.branch
        ) as span:
            try:
                result = await self._fetch(key, repo.full_name, watch.etag)
            except RateLimitedError as exc:
                span.set_attribute("result", "rate_limited")
                await self._reschedule(key, exc.retry_at)
                return 0
            except NotFoundError:
                # Deleted, made private, or branch removed. Keep checking, but rarely.
                span.set_attribute("result", "not_found")
                logfire.warn("{repo} {kind} returned 404", repo=repo.full_name, kind=key.kind)
                await self._reschedule(key, self._at(self._settings.poll_max_interval))
                return 0
            except GitHubError as exc:
                span.set_attribute("result", "error")
                logfire.warn(
                    "GitHub error polling {repo}: {error}", repo=repo.full_name, error=str(exc)
                )
                await self._reschedule(key, self._at(watch.poll_interval))
                return 0

            span.set_attribute("result", 304 if result.not_modified else 200)
            if result.redirected:
                await self._refresh_repo(repo)

            if result.items is None:
                detection = Detection(watermark=watch.last_seen_id or "")
            elif watch.last_seen_id is None:
                detection = Detection(watermark=_baseline(key.kind, result.items))
            else:
                detection = _detect(key, RepoRef(repo.full_name), result.items, watch.last_seen_id)

            interval = next_interval(
                watch.poll_interval,
                changed=bool(detection.events),
                min_interval=self._settings.poll_min_interval,
                max_interval=self._settings.poll_max_interval,
            )
            new_deliveries = 0
            async with self._sessions.begin() as session:
                for event in detection.events:
                    event_id = await queries.insert_event(
                        session,
                        repo_id=key.repo_id,
                        kind=key.kind,
                        external_id=event.external_id,
                        payload=event.payload,
                    )
                    if event_id is None:
                        continue  # already recorded by an earlier poll
                    new_deliveries += await queries.fan_out_event(
                        session,
                        event_id=event_id,
                        key=key,
                        default_branch=repo.default_branch,
                        prerelease=event.prerelease,
                    )
                await queries.update_watch(
                    session,
                    key,
                    etag=result.etag,
                    last_seen_id=detection.watermark,
                    poll_interval=interval,
                    next_poll_at=self._at(interval),
                )
            span.set_attributes({"events": len(detection.events), "deliveries": new_deliveries})
            return new_deliveries

    # ------------------------------------------------------------------ baselining

    async def baseline(self, key: WatchKey, full_name: str) -> list[Any]:
        """Record the current latest item as seen without announcing anything.

        Returns the fetched items so callers can show a preview.
        """
        with logfire.span("baseline {repo} {kind}", repo=full_name, kind=key.kind):
            result = await self._fetch(key, full_name, etag=None)
            items = result.items or []
            interval = self._settings.poll_default_interval
            async with self._sessions.begin() as session:
                await queries.update_watch(
                    session,
                    key,
                    etag=result.etag,
                    last_seen_id=_baseline(key.kind, items),
                    poll_interval=interval,
                    next_poll_at=self._at(interval),
                )
            return items

    # ------------------------------------------------------------------ helpers

    async def _fetch(self, key: WatchKey, full_name: str, etag: str | None) -> Conditional[Any]:
        match key.kind:
            case Kind.RELEASE:
                return await self._github.list_releases(full_name, etag=etag)
            case Kind.TAG:
                return await self._github.list_tags(full_name, etag=etag)
            case Kind.COMMIT:
                return await self._github.list_commits(full_name, key.branch, etag=etag)
        raise ValueError(f"unknown watch kind {key.kind!r}")

    def _at(self, interval: timedelta) -> datetime:
        return schedule_at(
            datetime.now(UTC),
            interval,
            self._github.rate_limit,
            self._settings.github_rate_limit_floor,
        )

    async def _reschedule(self, key: WatchKey, at: datetime) -> None:
        async with self._sessions.begin() as session:
            await queries.reschedule_watch(session, key, at)

    async def _refresh_repo(self, repo: Repo) -> None:
        """The repo was renamed or transferred; update its display name."""
        try:
            fresh = await self._github.get_repo_by_id(repo.github_id)
        except GitHubError:
            return
        async with self._sessions.begin() as session:
            await queries.update_repo_metadata(
                session, repo.id, full_name=fresh.full_name, default_branch=fresh.default_branch
            )
        logfire.info("repo {old} is now {new}", old=repo.full_name, new=fresh.full_name)


def _baseline(kind: str, items: list[Any]) -> str:
    match kind:
        case Kind.RELEASE:
            return detectors.baseline_releases(items)
        case Kind.TAG:
            return detectors.baseline_tags(items)
        case Kind.COMMIT:
            return detectors.baseline_commits(items)
    raise ValueError(f"unknown watch kind {kind!r}")


def _detect(key: WatchKey, repo: RepoRef, items: list[Any], last_seen: str) -> Detection:
    match key.kind:
        case Kind.RELEASE:
            return detectors.detect_releases(repo, items, last_seen)
        case Kind.TAG:
            return detectors.detect_tags(repo, items, last_seen)
        case Kind.COMMIT:
            return detectors.detect_commits(repo, key.branch, items, last_seen)
    raise ValueError(f"unknown watch kind {key.kind!r}")
