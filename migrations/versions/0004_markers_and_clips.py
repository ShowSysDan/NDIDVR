"""Markers + clip export queue for the DVR watch page

Revision ID: 0004_markers_clips
Revises: 0003_timelapse
Create Date: 2026-04-20 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_markers_clips"
down_revision = "0003_timelapse"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "markers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id", sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("timestamp_utc", sa.DateTime(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False, server_default=""),
        sa.Column(
            "color", sa.String(length=16),
            nullable=False, server_default="#22c55e",
        ),
        sa.Column(
            "created_at", sa.DateTime(),
            nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_markers_source_id", "markers", ["source_id"])
    op.create_index("ix_markers_timestamp_utc", "markers", ["timestamp_utc"])

    op.create_table(
        "clips",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id", sa.Integer(),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("label", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("start_utc", sa.DateTime(), nullable=False),
        sa.Column("end_utc", sa.DateTime(), nullable=False),
        sa.Column(
            "status", sa.String(length=16),
            nullable=False, server_default="queued",
        ),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_path", sa.String(length=1024), nullable=True),
        sa.Column("output_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(),
            nullable=False, server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_clips_source_id", "clips", ["source_id"])
    op.create_index("ix_clips_start_utc", "clips", ["start_utc"])
    op.create_index("ix_clips_status", "clips", ["status"])


def downgrade() -> None:
    op.drop_index("ix_clips_status", table_name="clips")
    op.drop_index("ix_clips_start_utc", table_name="clips")
    op.drop_index("ix_clips_source_id", table_name="clips")
    op.drop_table("clips")

    op.drop_index("ix_markers_timestamp_utc", table_name="markers")
    op.drop_index("ix_markers_source_id", table_name="markers")
    op.drop_table("markers")
