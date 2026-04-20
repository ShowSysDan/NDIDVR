"""Per-source timelapse interval

Revision ID: 0003_timelapse
Revises: 0002_audio_settings
Create Date: 2026-04-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0003_timelapse"
down_revision = "0002_audio_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0 means timelapse disabled. Default 0 so existing sources get no
    # behaviour change; opt-in is per-source via the settings page.
    op.add_column(
        "sources",
        sa.Column(
            "timelapse_interval_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("sources", "timelapse_interval_seconds")
