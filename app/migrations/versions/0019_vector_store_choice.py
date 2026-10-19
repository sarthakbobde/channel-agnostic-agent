"""Vector store per knowledge base (type and non-secret settings), recorded on every index version.

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    empty = sa.text("'{}'")
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.add_column(sa.Column("store", sa.String(20), nullable=False, server_default="chroma"))
        batch.add_column(sa.Column("store_config", sa.JSON, nullable=False, server_default=empty))
    with op.batch_alter_table("kb_index_versions") as batch:
        batch.add_column(sa.Column("store_config", sa.JSON, nullable=False, server_default=empty))


def downgrade() -> None:
    with op.batch_alter_table("kb_index_versions") as batch:
        batch.drop_column("store_config")
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.drop_column("store_config")
        batch.drop_column("store")
