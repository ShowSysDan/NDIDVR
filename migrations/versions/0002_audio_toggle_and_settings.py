"""Per-source audio toggle, settings KV table, and chunks indexes

Revision ID: 0002_audio_settings
Revises: 0001_initial
Create Date: 2026-04-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_audio_settings"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Per-source audio-recording toggle. Default True so existing rows keep
    # their current behaviour.
    op.add_column(
        "sources",
        sa.Column(
            "record_audio",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )

    # App-level key/value settings table for values editable at runtime from
    # the dashboard (retention days, compression schedule, etc.).
    op.create_table(
        "app_settings",
        sa.Column("key",        sa.String(64),   primary_key=True),
        sa.Column("value",      sa.String(1024), nullable=False),
        sa.Column("updated_at", sa.DateTime(),   nullable=True),
    )

    # Every dashboard page / API call filters or sorts by these columns.
    # Add indexes so an unindexed full scan can't be used as a cheap DoS.
    op.create_index("ix_chunks_started_at", "chunks", ["started_at"])
    op.create_index("ix_chunks_source_id",  "chunks", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_chunks_source_id",  table_name="chunks")
    op.drop_index("ix_chunks_started_at", table_name="chunks")
    op.drop_table("app_settings")
    op.drop_column("sources", "record_audio")
