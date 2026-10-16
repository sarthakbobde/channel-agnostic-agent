"""Documents: one row per uploaded file in a knowledge base, with its indexing status.

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kb_id", sa.Integer, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("doc_key", sa.String(240), nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("error", sa.Text, nullable=False),
        sa.Column("chunks", sa.Integer, nullable=False),
        sa.Column("uploaded_by", sa.String(254), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("kb_id", "name", name="uq_documents_kb_name"),
    )
    op.create_index("ix_documents_kb_id", "documents", ["kb_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_kb_id", table_name="documents")
    op.drop_table("documents")
