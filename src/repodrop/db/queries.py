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

from repodrop.db.models import (
    Delivery,
    DeliveryStatus,
    Event,
    GuildSettings,
    Kind,
    Repo,
    RepoWatch,
    Subscription,
)

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


@dataclass(frozen=True, slots=True)
class GuildUsage:
    repos: int  # distinct repos across the server's subscriptions
    subscriptions: int  # (channel, repo) pairs


async def guild_usage(session: AsyncSession, guild_id: int) -> GuildUsage:
    """Usage against the server's caps. Disabled subscriptions count too."""
    row = (
        await session.execute(
            select(func.count(Subscription.repo_id.distinct()), func.count()).where(
                Subscription.guild_id == guild_id
            )
        )
    ).one()
    return GuildUsage(repos=row[0], subscriptions=row[1])


async def lock_guild(session: AsyncSession, guild_id: int) -> None:
    """Take a transaction-scoped advisory lock for a server (released on commit/rollback)."""
    await session.execute(select(func.pg_advisory_xact_lock(guild_id)))


async def guild_follows_repo(session: AsyncSession, guild_id: int, repo_id: int) -> bool:
    """Whether any channel in the server already subscribes to this repo."""
    stmt = select(
        select(Subscription.id)
        .where(Subscription.guild_id == guild_id, Subscription.repo_id == repo_id)
        .exists()
    )
    return bool(await session.scalar(stmt))


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
    branches: list[str],
    include_prereleases: bool,
    created_by: int,
) -> tuple[Subscription, bool]:
    """Create the subscription, or overwrite the settings of an existing one.

    An existing subscription is reactivated if it was disabled. Returns
    `(subscription, created)`. Callers check who may update an existing one.
    """
    existing = await get_subscription(
        session, guild_id=guild_id, channel_id=channel_id, repo_id=repo_id
    )
    if existing is not None:
        existing.kinds = kinds
        existing.branches = branches
        existing.include_prereleases = include_prereleases
        existing.active = True
        existing.disabled_reason = None
        existing.disabled_at = None
        await session.flush()
        return existing, False

    sub = Subscription(
        guild_id=guild_id,
        channel_id=channel_id,
        repo_id=repo_id,
        kinds=kinds,
        branches=branches,
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
    session: AsyncSession,
    guild_id: int,
    query: str,
    *,
    created_by: int | None = None,
    limit: int = 25,
) -> list[str]:
    """Repo names subscribed in this server, optionally only subscriptions `created_by` made."""
    stmt = (
        select(Repo.full_name)
        .join(Subscription, Subscription.repo_id == Repo.id)
        .where(Subscription.guild_id == guild_id, Repo.full_name.ilike(f"%{_escape_like(query)}%"))
        .distinct()
        .order_by(Repo.full_name)
        .limit(limit)
    )
    if created_by is not None:
        stmt = stmt.where(Subscription.created_by == created_by)
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
                  AND (
                    w.kind <> 'commit'
                    OR w.branch = ANY (s.branches)
                    OR (cardinality(s.branches) = 0 AND w.branch = r.default_branch)
                  )
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


# --------------------------------------------------------------------------- guild settings


async def get_guild_settings(session: AsyncSession, guild_id: int) -> GuildSettings | None:
    return await session.get(GuildSettings, guild_id)


async def upsert_guild_settings(session: AsyncSession, guild_id: int, **values: Any) -> None:
    """Set the given columns for a server, creating its row if needed."""
    values["updated_at"] = func.now()
    await session.execute(
        insert(GuildSettings)
        .values(guild_id=guild_id, **values)
        .on_conflict_do_update(index_elements=[GuildSettings.guild_id], set_=values)
    )


async def delete_guild_settings_unless_blocked(session: AsyncSession, guild_id: int) -> None:
    """Blocked rows are kept so re-inviting the bot doesn't escape the block."""
    await session.execute(
        delete(GuildSettings).where(GuildSettings.guild_id == guild_id, ~GuildSettings.blocked)
    )


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
    """Create a pending delivery for every active subscription that wants this event.

    Subscriptions in blocked servers, and commit events in servers with commits disallowed, are
    skipped but kept, so lifting the restriction resumes delivery. A server with no settings row
    uses the defaults (not blocked, commits allowed).
    """
    # Typed cast: an untyped bind in a SELECT list would resolve to text in Postgres.
    matching = (
        select(cast(literal(event_id), BigInteger), Subscription.id)
        .outerjoin(GuildSettings, GuildSettings.guild_id == Subscription.guild_id)
        .where(
            Subscription.repo_id == key.repo_id,
            Subscription.active,
            Subscription.kinds.any_() == key.kind,
            ~func.coalesce(GuildSettings.blocked, False),
        )
    )
    if key.kind == Kind.COMMIT:
        branch_match = Subscription.branches.any_() == key.branch
        if key.branch == default_branch:
            # An empty branch list means "follow the default branch".
            branch_match = or_(branch_match, func.cardinality(Subscription.branches) == 0)
        matching = matching.where(branch_match, func.coalesce(GuildSettings.commits_allowed, True))
    if prerelease:
        matching = matching.where(Subscription.include_prereleases)

    stmt = (
        insert(Delivery)
        .from_select([Delivery.event_id, Delivery.subscription_id], matching)
        .on_conflict_do_nothing()
    )
    result = await session.execute(stmt)
    return result.rowcount  # type: ignore[attr-defined]


async def skip_removed_deliveries(
    session: AsyncSession,
    subscription_id: int,
    *,
    kinds: Sequence[str] = (),
    commit_branches: Sequence[str] = (),
    prereleases: bool = False,
) -> int:
    """Mark pending deliveries `skipped` for what an update just stopped announcing."""
    conditions = []
    if kinds:
        conditions.append(Event.kind.in_(kinds))
    if commit_branches:
        conditions.append(
            (Event.kind == Kind.COMMIT) & Event.payload["branch"].astext.in_(commit_branches)
        )
    if prereleases:
        conditions.append(
            (Event.kind == Kind.RELEASE) & (Event.payload["prerelease"].astext == "true")
        )
    if not conditions:
        return 0
    result = await session.execute(
        update(Delivery)
        .where(
            Delivery.event_id == Event.id,
            Delivery.subscription_id == subscription_id,
            Delivery.status == DeliveryStatus.PENDING,
            or_(*conditions),
        )
        .values(status=DeliveryStatus.SKIPPED, last_error="removed from subscription")
    )
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
