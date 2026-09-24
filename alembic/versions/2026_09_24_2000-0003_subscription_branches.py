"""subscription branch lists and disable tracking

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "branches",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # NULL branch meant "the default branch", which is now an empty list.
    op.execute("UPDATE subscriptions SET branches = ARRAY[branch] WHERE branch IS NOT NULL")
    op.drop_column("subscriptions", "branch")

    op.add_column("subscriptions", sa.Column("disabled_reason", sa.Text(), nullable=True))
    op.add_column(
        "subscriptions",
        sa.Column("disabled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "disabled_at")
    op.drop_column("subscriptions", "disabled_reason")
    op.add_column("subscriptions", sa.Column("branch", sa.Text(), nullable=True))
    # Only the first branch survives a downgrade.
    op.execute("UPDATE subscriptions SET branch = branches[1] WHERE cardinality(branches) > 0")
    op.drop_column("subscriptions", "branches")
