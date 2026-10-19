"""Audit targets can be longer than 64 characters (a knowledge base name plus a file name).

Revision ID: 0021
Revises: 0020
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.alter_column("target", existing_type=sa.String(64), type_=sa.String(254), existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("audit_log") as batch:
        batch.alter_column("target", existing_type=sa.String(254), type_=sa.String(64), existing_nullable=False)
