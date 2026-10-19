"""Who may ask questions of a knowledge base: no rows = everyone on a channel it is bound to;
rows = only members of those groups (D17).

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_access",
        sa.Column("kb_id", sa.Integer, primary_key=True),
        sa.Column("group_id", sa.Integer, primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("kb_access")
