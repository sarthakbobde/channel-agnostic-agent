"""Chunking strategy per knowledge base, and recorded on every index version.

Revision ID: 0017
Revises: 0016
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.add_column(sa.Column("chunk_strategy", sa.String(16), nullable=False, server_default="recursive"))
    with op.batch_alter_table("kb_index_versions") as batch:
        batch.add_column(sa.Column("chunk_strategy", sa.String(16), nullable=False, server_default="recursive"))


def downgrade() -> None:
    with op.batch_alter_table("kb_index_versions") as batch:
        batch.drop_column("chunk_strategy")
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.drop_column("chunk_strategy")
