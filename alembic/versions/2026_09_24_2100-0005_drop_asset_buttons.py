"""drop guild_settings.show_asset_buttons (download buttons were removed)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("guild_settings", "show_asset_buttons")


def downgrade() -> None:
    op.add_column(
        "guild_settings",
        sa.Column(
            "show_asset_buttons", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
    )
