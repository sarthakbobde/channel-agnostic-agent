"""A per-person limit on questions per minute (Global Settings).

Revision ID: 0022
Revises: 0021
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("global_settings") as batch:
        batch.add_column(sa.Column("chat_rate_per_minute", sa.Integer, nullable=False, server_default="20"))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as batch:
        batch.drop_column("chat_rate_per_minute")
