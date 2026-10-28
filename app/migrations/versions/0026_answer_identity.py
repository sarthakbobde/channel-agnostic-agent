"""Release 2 groundwork: who asked each question, and which answer each model call belongs to.

`messages.author_user_id` / `author_email` record the person who asked (several people can ask in one Slack or
Teams thread, so the conversation owner is not enough). `usage_records.message_id` links a model call to the
answer it served; `purpose` is widened so longer purposes fit on PostgreSQL.

Revision ID: 0026
Revises: 0025
"""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("author_user_id", sa.Integer, nullable=True))
        batch.add_column(sa.Column("author_email", sa.String(254), nullable=False, server_default=""))
    with op.batch_alter_table("usage_records") as batch:
        batch.alter_column("purpose", type_=sa.String(32), existing_type=sa.String(16), existing_nullable=False)
        batch.add_column(sa.Column("message_id", sa.Integer, nullable=True))
        batch.create_index("ix_usage_records_message_id", ["message_id"])


def downgrade() -> None:
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_index("ix_usage_records_message_id")
        batch.drop_column("message_id")
        batch.alter_column("purpose", type_=sa.String(16), existing_type=sa.String(32), existing_nullable=False)
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("author_email")
        batch.drop_column("author_user_id")
