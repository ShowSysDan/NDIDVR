"""Initial schema — sources and chunks tables

Revision ID: 0001_initial
Revises:
Create Date: 2025-04-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id",           sa.Integer(),     nullable=False, primary_key=True),
        sa.Column("ndi_name",     sa.String(255),   nullable=False, unique=True),
        sa.Column("display_name", sa.String(255),   nullable=True),
        sa.Column("enabled",      sa.Boolean(),     nullable=False, server_default=sa.true()),
        sa.Column("quality",      sa.String(32),    nullable=False, server_default="archive"),
        sa.Column("first_seen",   sa.DateTime(),    nullable=True),
        sa.Column("last_seen",    sa.DateTime(),    nullable=True),
    )

    op.create_table(
        "chunks",
        sa.Column("id",                   sa.Integer(),    nullable=False, primary_key=True),
        sa.Column("source_id",            sa.Integer(),    sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("started_at",           sa.DateTime(),   nullable=False),
        sa.Column("ended_at",             sa.DateTime(),   nullable=True),
        sa.Column("duration_seconds",     sa.Float(),      nullable=True),
        sa.Column("quality",              sa.String(32),   nullable=False, server_default="archive"),
        sa.Column("local_path",           sa.String(1024), nullable=True),
        sa.Column("s3_key",               sa.String(1024), nullable=True),
        sa.Column("s3_bucket",            sa.String(255),  nullable=True),
        sa.Column("size_bytes",           sa.BigInteger(), nullable=True),
        sa.Column("upload_status",        sa.String(32),   nullable=False, server_default="pending"),
        sa.Column("uploaded_at",          sa.DateTime(),   nullable=True),
        sa.Column("compressed",           sa.Boolean(),    nullable=False, server_default=sa.false()),
        sa.Column("compressed_s3_key",    sa.String(1024), nullable=True),
        sa.Column("compressed_size_bytes",sa.BigInteger(), nullable=True),
        sa.Column("created_at",           sa.DateTime(),   nullable=True),
    )

    # Indexes for common query patterns
    op.create_index("ix_chunks_source_id",     "chunks", ["source_id"])
    op.create_index("ix_chunks_started_at",    "chunks", ["started_at"])
    op.create_index("ix_chunks_upload_status", "chunks", ["upload_status"])
    op.create_index("ix_chunks_local_path",    "chunks", ["local_path"])
    op.create_index("ix_sources_ndi_name",     "sources", ["ndi_name"], unique=True)


def downgrade() -> None:
    op.drop_table("chunks")
    op.drop_table("sources")
