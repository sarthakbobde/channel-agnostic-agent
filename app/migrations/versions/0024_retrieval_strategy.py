"""How passages are found: similarity, keyword, hybrid or similarity with variety.

Revision ID: 0024
Revises: 0023
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("global_settings") as batch:
        batch.add_column(sa.Column("retrieval_strategy", sa.String(16), nullable=False, server_default="similarity"))
        batch.add_column(sa.Column("hybrid_keyword_weight", sa.Float, nullable=False, server_default="0.5"))
        batch.add_column(sa.Column("diversity", sa.Float, nullable=False, server_default="0.3"))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as batch:
        batch.drop_column("diversity")
        batch.drop_column("hybrid_keyword_weight")
        batch.drop_column("retrieval_strategy")
