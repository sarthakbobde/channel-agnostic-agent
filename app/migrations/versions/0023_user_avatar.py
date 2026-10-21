"""A profile photo per person (shown in the admin center's top bar and Account screen).

Revision ID: 0023
Revises: 0022
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("avatar", sa.LargeBinary, nullable=True))
        batch.add_column(sa.Column("avatar_type", sa.String(32), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("avatar_type")
        batch.drop_column("avatar")
