"""Personal data masked in documents at indexing (12c) and caught at search time (12c+).

`knowledge_bases.pii_mask`: mask personal data in this knowledge base's documents. Off for the knowledge bases that
already exist (an Admin switches it on, which needs a rebuild); new knowledge bases start with it on.
`kb_index_versions.pii_mask`: what an index version was built with, so a change shows the index as out of date.
`documents.pii_counts`: {type: how many} masked at the last indexing (never values).
`documents.pii_flagged`: the search-time filter found personal data the indexing missed; re-index the document.

Revision ID: 0027
Revises: 0026
"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.add_column(sa.Column("pii_mask", sa.Boolean, nullable=False, server_default=sa.false()))
    with op.batch_alter_table("kb_index_versions") as batch:
        batch.add_column(sa.Column("pii_mask", sa.Boolean, nullable=False, server_default=sa.false()))
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("pii_counts", sa.JSON, nullable=False, server_default=sa.text("'{}'")))
        batch.add_column(sa.Column("pii_flagged", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("pii_flagged")
        batch.drop_column("pii_counts")
    with op.batch_alter_table("kb_index_versions") as batch:
        batch.drop_column("pii_mask")
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.drop_column("pii_mask")
