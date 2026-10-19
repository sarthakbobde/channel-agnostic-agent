"""Settings for long-chat summaries, the retention job and the conversation export.

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("global_settings") as batch:
        batch.add_column(sa.Column("summarise_after_turns", sa.Integer, nullable=False, server_default="12"))
        batch.add_column(sa.Column("retention_job_enabled", sa.Boolean, nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("export_enabled", sa.Boolean, nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as batch:
        batch.drop_column("export_enabled")
        batch.drop_column("retention_job_enabled")
        batch.drop_column("summarise_after_turns")
