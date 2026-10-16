"""Index versions: each knowledge base has an active index version; a rebuild creates a new one and
switches only when complete. The previous version is kept for rollback.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_index_versions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kb_id", sa.Integer, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("collection", sa.String(80), nullable=False),
        sa.Column("chunk_size", sa.Integer, nullable=False),
        sa.Column("chunk_overlap", sa.Integer, nullable=False),
        sa.Column("embedding", sa.String(40), nullable=False),
        sa.Column("store", sa.String(20), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("chunks", sa.Integer, nullable=False),
        sa.Column("documents", sa.Integer, nullable=False),
        sa.Column("progress_done", sa.Integer, nullable=False),
        sa.Column("progress_total", sa.Integer, nullable=False),
        sa.Column("error", sa.Text, nullable=False),
        sa.Column("created_by", sa.String(254), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("seconds", sa.Float),
        sa.UniqueConstraint("kb_id", "version", name="uq_kb_index_version"),
    )
    op.create_index("ix_kb_index_versions_kb_id", "kb_index_versions", ["kb_id"])


def downgrade() -> None:
    op.drop_index("ix_kb_index_versions_kb_id", table_name="kb_index_versions")
    op.drop_table("kb_index_versions")
