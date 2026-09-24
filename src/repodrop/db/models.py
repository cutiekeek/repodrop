from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Interval,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Kind(StrEnum):
    RELEASE = "release"
    TAG = "tag"
    COMMIT = "commit"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class Base(DeclarativeBase):
    type_annotation_map: ClassVar = {datetime: TIMESTAMP(timezone=True)}


class Repo(Base):
    """One row per GitHub repository anyone is watching."""

    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    full_name: Mapped[str] = mapped_column(Text)  # display only; refreshed on poll
    default_branch: Mapped[str] = mapped_column(Text)
    metadata_etag: Mapped[str | None] = mapped_column(Text)  # for the daily metadata refresh
    metadata_checked_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RepoWatch(Base):
    """Poll state per (repo, kind, branch). Only kinds with an active subscription are kept."""

    __tablename__ = "repo_watches"

    repo_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    branch: Mapped[str] = mapped_column(Text, primary_key=True, server_default="")  # 'commit' only
    etag: Mapped[str | None] = mapped_column(Text)
    # Detection watermark: newest release's published_at, newest tag name, or head commit SHA.
    # NULL means the watch has not been baselined yet.
    last_seen_id: Mapped[str | None] = mapped_column(Text)
    poll_interval: Mapped[timedelta] = mapped_column(Interval, server_default=text("'10 minutes'"))
    # 'infinity' means stopped (e.g. the branch was deleted); re-subscribing resets it.
    next_poll_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    last_polled_at: Mapped[datetime | None] = mapped_column()  # shown by /github status
    last_error: Mapped[str | None] = mapped_column(Text)  # cleared by the next successful poll
    notified_at: Mapped[datetime | None] = mapped_column()  # admin notice sent for a stop


class Subscription(Base):
    """A channel's subscription to a repo."""

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("guild_id", "channel_id", "repo_id"),
        Index("ix_subscriptions_repo_id_active", "repo_id", postgresql_where=text("active")),
        Index("ix_subscriptions_guild_id", "guild_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    repo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("repos.id"))
    kinds: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{release}'"))
    # Commit branches; empty = follow the repo's default branch.
    branches: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    include_prereleases: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    disabled_reason: Mapped[str | None] = mapped_column(Text)  # set when auto-disabled
    disabled_at: Mapped[datetime | None] = mapped_column()
    notified_at: Mapped[datetime | None] = mapped_column()  # admin notice sent for this disable
    created_by: Mapped[int] = mapped_column(BigInteger)  # Discord user ID
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Event(Base):
    """A detected update. The unique key makes detection idempotent."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("repo_id", "kind", "external_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("repos.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)  # release ID / tag name / push range
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    detected_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Delivery(Base):
    """Outbox: one row per (event, subscription)."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "subscription_id"),
        Index(
            "ix_deliveries_next_attempt_at_pending",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_deliveries_subscription_id_sent_at_sent",
            "subscription_id",
            text("sent_at DESC"),
            postgresql_where=text("status = 'sent'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id", ondelete="CASCADE"))
    subscription_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subscriptions.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, server_default=DeliveryStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)


class EmbedStyle(StrEnum):
    FULL = "full"
    COMPACT = "compact"


class LatestAccess(StrEnum):
    EVERYONE = "everyone"
    MANAGERS = "managers"


class GuildSettings(Base):
    """Per-server settings. A missing row means every setting uses its default.

    Nullable limit columns fall back to the global config value.
    """

    __tablename__ = "guild_settings"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # Operator-controlled (owner commands only)
    max_repos: Mapped[int | None] = mapped_column(Integer)
    max_subscriptions: Mapped[int | None] = mapped_column(Integer)
    commits_allowed: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    blocked: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    blocked_at: Mapped[datetime | None] = mapped_column()

    # Admin-controlled (/github settings)
    manager_role_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL = Manage Server only
    subscriber_role_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL = everyone
    default_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    embed_style: Mapped[str] = mapped_column(Text, server_default=EmbedStyle.FULL.value)
    show_asset_buttons: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    latest_access: Mapped[str] = mapped_column(Text, server_default=LatestAccess.EVERYONE.value)
    latest_allow_public: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
