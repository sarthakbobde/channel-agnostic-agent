"""Alerts can be meant for one person as well as the Admins (for example the uploader of a document that failed to index).

Revision ID: 0020
Revises: 0019
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alerts") as batch:
        batch.add_column(sa.Column("audience", sa.String(254), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("alerts") as batch:
        batch.drop_column("audience")
