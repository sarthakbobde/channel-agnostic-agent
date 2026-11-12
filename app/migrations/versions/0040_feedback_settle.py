"""Release 3: wait before processing a feedback (found in the first live Slack test: the comment arrives after the rating).

Revision ID: 0040
Revises: 0039
"""
import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        b.add_column(sa.Column("feedback_settle_minutes", sa.Integer, nullable=False, server_default="2"))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        b.drop_column("feedback_settle_minutes")
