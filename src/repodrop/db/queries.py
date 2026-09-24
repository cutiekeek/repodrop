"""Subscription, watch, event, and delivery queries.

All functions take an open session and leave transaction control to the caller.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, cast, delete, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from repodrop.db.models import Delivery, DeliveryStatus, Event, Kind, Repo, RepoWatch, Subscription

# --------------------------------------------------------------------------- repos


async def upsert_repo(
    session: AsyncSession, *, github_id: int, full_name: str, default_branch: str
) -> Repo:
    stmt = (
        insert(Repo)
        .values(github_id=github_id, full_name=full_name, default_branch=default_branch)
        .on_conflict_do_update(
            index_elements=[Repo.github_id],
            set_={"full_name": full_name, "default_branch": default_branch},
        )
        .returning(Repo)
        .execution_options(populate_existing=True)
    )
    return (await session.scalars(stmt)).one()


async def get_repos(session: AsyncSession, repo_ids: Sequence[int]) -> dict[int, Repo]:
    repos = await session.scalars(select(Repo).where(Repo.id.in_(repo_ids)))
    return {r.id: r for r in repos}


async def update_repo_metadata(
    session: AsyncSession, repo_id: int, *, full_name: str, default_branch: str
) -> None:
    await session.execute(
        update(Repo)
        .where(Repo.id == repo_id)
        .values(full_name=full_name, default_branch=default_branch)
    )


# --------------------------------------------------------------------------- subscriptions


async def count_active_guild_subscriptions(session: AsyncSession, guild_id: int) -> int:
    stmt = select(func.count()).where(Subscription.guild_id == guild_id, Subscription.active)
    return (await session.scalar(stmt)) or 0


async def get_subscription(
    session: AsyncSession, *, guild_id: int, channel_id: int, repo_id: int
) -> Subscription | None:
    return await session.scalar(
        select(Subscription).where(
            Subscription.guild_id == guild_id,
            Subscription.channel_id == channel_id,
            Subscription.repo_id == repo_id,
        )
    )


async def upsert_subscription(
    session: AsyncSession,
    *,
    guild_id: int,
    channel_id: int,
    repo_id: int,
    kinds: list[str],
    branch: str | None,
    include_prereleases: bool,
    created_by: int,
) -> tuple[Subscription, bool]:
    """Create the subscription, or overwrite the settings of an existing one.

    Returns `(subscription, created)`.
    """
    existing = await get_subscription(
        session, guild_id=guild_id, channel_id=channel_id, repo_id=repo_id
    )
    if existing is not None:
        existing.kinds = kinds
        existing.branch = branch
        existing.include_prereleases = include_prereleases
        existing.active = True
        await session.flush()
        return existing, False

    sub = Subscription(
        guild_id=guild_id,
        channel_id=channel_id,
        repo_id=repo_id,
        kinds=kinds,
        branch=branch,
        include_prereleases=include_prereleases,
        created_by=created_by,
    )
    session.add(sub)
    await session.flush()
    return sub, True


async def list_subscriptions(
    session: AsyncSession, guild_id: int, channel_id: int | None = None
) -> list[tuple[Subscription, Repo]]:
    stmt = (
        select(Subscription, Repo)
        .join(Repo, Repo.id == Subscription.repo_id)
        .where(Subscription.guild_id == guild_id)
        .order_by(Subscription.channel_id, func.lower(Repo.full_name))
    )
    if channel_id is not None:
        stmt = stmt.where(Subscription.channel_id == channel_id)
    return [(s, r) for s, r in (await session.execute(stmt)).tuples()]


async def find_subscription_by_repo_name(
    session: AsyncSession, *, guild_id: int, channel_id: int, full_name: str
) -> Subscription | None:
    stmt = (
        select(Subscription)
        .join(Repo, Repo.id == Subscription.repo_id)
        .where(
            Subscription.guild_id == guild_id,
            Subscription.channel_id == channel_id,
            func.lower(Repo.full_name) == full_name.lower(),
        )
    )
    return await session.scalar(stmt)


async def search_guild_repo_names(
    session: AsyncSession, guild_id: int, query: str, limit: int = 25
) -> list[str]:
    stmt = (
        select(Repo.full_name)
        .join(Subscription, Subscription.repo_id == Repo.id)
        .where(Subscription.guild_id == guild_id, Repo.full_name.ilike(f"%{_escape_like(query)}%"))
        .distinct()
        .order_by(Repo.full_name)
        .limit(limit)
    )
    return list(await session.scalars(stmt))


async def delete_subscription(session: AsyncSession, subscription_id: int) -> None:
    await session.execute(delete(Subscription).where(Subscription.id == subscription_id))


async def delete_guild_subscriptions(session: AsyncSession, guild_id: int) -> int:
    result = await session.execute(delete(Subscription).where(Subscription.guild_id == guild_id))
    return result.rowcount  # type: ignore[attr-defined]


async def deactivate_subscription(session: AsyncSession, subscription_id: int) -> None:
    await session.execute(
        update(Subscription).where(Subscription.id == subscription_id).values(active=False)
    )


async def prune_orphans(session: AsyncSession) -> tuple[int, int]:
    """Drop watches no active subscription needs, then repos with no subscriptions at all.

    Returns `(watches_deleted, repos_deleted)`.
    """
    watches = await session.execute(
        text(
            """
            DELETE FROM repo_watches w
            USING repos r
            WHERE r.id = w.repo_id
              AND NOT EXISTS (
                SELECT 1 FROM subscriptions s
                WHERE s.repo_id = w.repo_id
                  AND s.active
                  AND w.kind = ANY (s.kinds)
                  AND (w.kind <> 'commit' OR COALESCE(s.branch, r.default_branch) = w.branch)
              )
            """
        )
    )
    repos = await session.execute(
        text(
            """
            DELETE FROM repos r
            WHERE NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.repo_id = r.id)
            """
        )
    )
    return watches.rowcount, repos.rowcount  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- watches


@dataclass(frozen=True, slots=True)
class WatchKey:
    repo_id: int
    kind: str
    branch: str


@dataclass(slots=True)
class ClaimedWatch:
    key: WatchKey
    etag: str | None
    last_seen_id: str | None
    poll_interval: timedelta


async def ensure_watch(
    session: AsyncSession, key: WatchKey, *, initial_interval: timedelta
) -> None:
    await session.execute(
        insert(RepoWatch)
        .values(
            repo_id=key.repo_id,
            kind=key.kind,
            branch=key.branch,
            poll_interval=initial_interval,
            # Give the subscribe flow a chance to baseline before the poller picks it up.
            next_poll_at=func.now() + initial_interval,
        )
        .on_conflict_do_nothing()
    )


async def get_watch(session: AsyncSession, key: WatchKey) -> RepoWatch | None:
    return await session.get(RepoWatch, (key.repo_id, key.kind, key.branch))


async def claim_due_watches(
    session: AsyncSession, *, limit: int, lease: timedelta
) -> list[ClaimedWatch]:
    """Pick due watches and push their `next_poll_at` out by `lease` so nothing else polls them.

    The poller overwrites `next_poll_at` when it finishes; if it crashes, the lease expires
    and the watch is retried.
    """
    due = (
        select(RepoWatch.repo_id, RepoWatch.kind, RepoWatch.branch)
        .where(RepoWatch.next_poll_at <= func.now())
        .order_by(RepoWatch.next_poll_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .subquery()
    )
    stmt = (
        update(RepoWatch)
        .where(
            RepoWatch.repo_id == due.c.repo_id,
            RepoWatch.kind == due.c.kind,
            RepoWatch.branch == due.c.branch,
        )
        .values(next_poll_at=func.now() + lease)
        .returning(
            RepoWatch.repo_id,
            RepoWatch.kind,
            RepoWatch.branch,
            RepoWatch.etag,
            RepoWatch.last_seen_id,
            RepoWatch.poll_interval,
        )
    )
    rows = (await session.execute(stmt)).all()
    return [
        ClaimedWatch(
            key=WatchKey(r.repo_id, r.kind, r.branch),
            etag=r.etag,
            last_seen_id=r.last_seen_id,
            poll_interval=r.poll_interval,
        )
        for r in rows
    ]


async def oldest_due_poll_lag(session: AsyncSession) -> timedelta | None:
    return await session.scalar(
        select(func.now() - func.min(RepoWatch.next_poll_at)).where(
            RepoWatch.next_poll_at <= func.now()
        )
    )


async def update_watch(
    session: AsyncSession,
    key: WatchKey,
    *,
    etag: str | None,
    last_seen_id: str | None,
    poll_interval: timedelta,
    next_poll_at: datetime,
) -> None:
    await session.execute(
        update(RepoWatch)
        .where(
            RepoWatch.repo_id == key.repo_id,
            RepoWatch.kind == key.kind,
            RepoWatch.branch == key.branch,
        )
        .values(
            etag=etag,
            last_seen_id=last_seen_id,
            poll_interval=poll_interval,
            next_poll_at=next_poll_at,
        )
    )


async def reschedule_watch(session: AsyncSession, key: WatchKey, next_poll_at: datetime) -> None:
    await session.execute(
        update(RepoWatch)
        .where(
            RepoWatch.repo_id == key.repo_id,
            RepoWatch.kind == key.kind,
            RepoWatch.branch == key.branch,
        )
        .values(next_poll_at=next_poll_at)
    )


# --------------------------------------------------------------------------- events


async def insert_event(
    session: AsyncSession, *, repo_id: int, kind: str, external_id: str, payload: dict[str, Any]
) -> int | None:
    """Insert an event; returns its ID, or None if it was already recorded."""
    stmt = (
        insert(Event)
        .values(repo_id=repo_id, kind=kind, external_id=external_id, payload=payload)
        .on_conflict_do_nothing(index_elements=[Event.repo_id, Event.kind, Event.external_id])
        .returning(Event.id)
    )
    return await session.scalar(stmt)


async def fan_out_event(
    session: AsyncSession,
    *,
    event_id: int,
    key: WatchKey,
    default_branch: str,
    prerelease: bool = False,
) -> int:
    """Create a pending delivery for every active subscription that wants this event."""
    # Typed cast: an untyped bind in a SELECT list would resolve to text in Postgres.
    matching = select(cast(literal(event_id), BigInteger), Subscription.id).where(
        Subscription.repo_id == key.repo_id,
        Subscription.active,
        Subscription.kinds.any_() == key.kind,
    )
    if key.kind == Kind.COMMIT:
        # A NULL subscription branch means "the default branch".
        branch_match = Subscription.branch == key.branch
        if key.branch == default_branch:
            branch_match = or_(branch_match, Subscription.branch.is_(None))
        matching = matching.where(branch_match)
    if prerelease:
        matching = matching.where(Subscription.include_prereleases)

    stmt = (
        insert(Delivery)
        .from_select([Delivery.event_id, Delivery.subscription_id], matching)
        .on_conflict_do_nothing()
    )
    result = await session.execute(stmt)
    return result.rowcount  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- deliveries


@dataclass(slots=True)
class ClaimedDelivery:
    id: int
    attempts: int
    subscription_id: int
    guild_id: int
    channel_id: int
    subscription_active: bool
    event_kind: str
    payload: dict[str, Any]


async def claim_deliveries(
    session: AsyncSession, *, limit: int, lease: timedelta
) -> list[ClaimedDelivery]:
    """Claim due pending deliveries, counting the attempt and leasing them for `lease`."""
    due = (
        select(Delivery.id)
        .where(Delivery.status == DeliveryStatus.PENDING, Delivery.next_attempt_at <= func.now())
        .order_by(Delivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .subquery()
    )
    claimed = (
        update(Delivery)
        .where(Delivery.id == due.c.id)
        .values(attempts=Delivery.attempts + 1, next_attempt_at=func.now() + lease)
        .returning(Delivery.id, Delivery.attempts, Delivery.event_id, Delivery.subscription_id)
        .cte("claimed")
    )
    stmt = (
        select(
            claimed.c.id,
            claimed.c.attempts,
            claimed.c.subscription_id,
            Subscription.guild_id,
            Subscription.channel_id,
            Subscription.active,
            Event.kind,
            Event.payload,
        )
        .select_from(claimed)
        .join(Subscription, Subscription.id == claimed.c.subscription_id)
        .join(Event, Event.id == claimed.c.event_id)
        .order_by(Event.detected_at, claimed.c.id)
    )
    rows = (await session.execute(stmt)).all()
    return [
        ClaimedDelivery(
            id=r.id,
            attempts=r.attempts,
            subscription_id=r.subscription_id,
            guild_id=r.guild_id,
            channel_id=r.channel_id,
            subscription_active=r.active,
            event_kind=r.kind,
            payload=r.payload,
        )
        for r in rows
    ]


async def mark_delivery_sent(session: AsyncSession, delivery_id: int, message_id: int) -> None:
    await session.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id)
        .values(status=DeliveryStatus.SENT, message_id=message_id, last_error=None)
    )


async def mark_delivery_skipped(session: AsyncSession, delivery_id: int, reason: str) -> None:
    await session.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id)
        .values(status=DeliveryStatus.SKIPPED, last_error=reason)
    )


async def mark_delivery_failed(session: AsyncSession, delivery_id: int, error: str) -> None:
    await session.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id)
        .values(status=DeliveryStatus.FAILED, last_error=error)
    )


async def schedule_delivery_retry(
    session: AsyncSession, delivery_id: int, *, error: str, delay: timedelta
) -> None:
    await session.execute(
        update(Delivery)
        .where(Delivery.id == delivery_id)
        .values(last_error=error, next_attempt_at=func.now() + delay)
    )


async def pending_delivery_count(session: AsyncSession) -> int:
    stmt = select(func.count()).where(Delivery.status == DeliveryStatus.PENDING)
    return (await session.scalar(stmt)) or 0


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
