"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-24 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("github_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "repo_watches",
        sa.Column(
            "repo_id",
            sa.BigInteger(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False, server_default=""),
        sa.Column("etag", sa.Text()),
        sa.Column("last_seen_id", sa.Text()),
        sa.Column(
            "poll_interval", sa.Interval(), nullable=False, server_default=sa.text("'10 minutes'")
        ),
        sa.Column("next_poll_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("repo_id", "kind", "branch"),
    )
    op.create_index("ix_repo_watches_next_poll_at", "repo_watches", ["next_poll_at"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_id", sa.BigInteger(), sa.ForeignKey("repos.id"), nullable=False),
        sa.Column(
            "kinds",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{release}'"),
        ),
        sa.Column("branch", sa.Text()),
        sa.Column(
            "include_prereleases", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("guild_id", "channel_id", "repo_id"),
    )
    op.create_index(
        "ix_subscriptions_repo_id_active",
        "subscriptions",
        ["repo_id"],
        postgresql_where=sa.text("active"),
    )
    op.create_index("ix_subscriptions_guild_id", "subscriptions", ["guild_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.BigInteger(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("detected_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("repo_id", "kind", "external_id"),
    )

    op.create_table(
        "deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "event_id",
            sa.BigInteger(),
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.BigInteger(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("event_id", "subscription_id"),
    )
    op.create_index(
        "ix_deliveries_next_attempt_at_pending",
        "deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("deliveries")
    op.drop_table("events")
    op.drop_table("subscriptions")
    op.drop_table("repo_watches")
    op.drop_table("repos")
