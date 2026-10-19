"""Embedding model per knowledge base (and on every index version).

Revision ID: 0018
Revises: 0017
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    empty = sa.text("'{}'")
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.add_column(sa.Column("embedding", sa.String(40), nullable=False, server_default="chroma-default"))
        batch.add_column(sa.Column("embedding_config", sa.JSON, nullable=False, server_default=empty))
    with op.batch_alter_table("kb_index_versions") as batch:
        batch.add_column(sa.Column("embedding_config", sa.JSON, nullable=False, server_default=empty))


def downgrade() -> None:
    with op.batch_alter_table("kb_index_versions") as batch:
        batch.drop_column("embedding_config")
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.drop_column("embedding_config")
        batch.drop_column("embedding")
