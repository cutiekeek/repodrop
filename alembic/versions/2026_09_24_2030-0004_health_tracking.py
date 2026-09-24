"""poll health, disable notices, repo metadata refresh, delivery sent_at

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24 20:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.add_column("repos", sa.Column("metadata_etag", sa.Text()))
    op.add_column("repos", sa.Column("metadata_checked_at", TS))

    op.add_column("repo_watches", sa.Column("last_polled_at", TS))
    op.add_column("repo_watches", sa.Column("last_error", sa.Text()))
    op.add_column("repo_watches", sa.Column("notified_at", TS))

    op.add_column("subscriptions", sa.Column("notified_at", TS))

    op.add_column("deliveries", sa.Column("sent_at", TS))
    op.create_index(
        "ix_deliveries_subscription_id_sent_at_sent",
        "deliveries",
        ["subscription_id", sa.text("sent_at DESC")],
        postgresql_where=sa.text("status = 'sent'"),
    )


def downgrade() -> None:
    op.drop_index("ix_deliveries_subscription_id_sent_at_sent", table_name="deliveries")
    op.drop_column("deliveries", "sent_at")
    op.drop_column("subscriptions", "notified_at")
    op.drop_column("repo_watches", "notified_at")
    op.drop_column("repo_watches", "last_error")
    op.drop_column("repo_watches", "last_polled_at")
    op.drop_column("repos", "metadata_checked_at")
    op.drop_column("repos", "metadata_etag")
