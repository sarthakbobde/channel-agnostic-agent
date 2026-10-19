"""Job locks: lets scheduled jobs run once even if two processes start by mistake (decision D11).

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_locks",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("job_locks")
