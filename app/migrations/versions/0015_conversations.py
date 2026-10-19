"""Conversations and messages (conversation memory moves from text files to the database).

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(200), primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("owner_user_id", sa.Integer),
        sa.Column("owner_email", sa.String(254), nullable=False),
        sa.Column("department_id", sa.Integer),
        sa.Column("department", sa.String(80), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("summarised_upto", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("conversation_id", sa.String(200), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("meta", sa.JSON, nullable=False),
        sa.Column("complete", sa.Boolean, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.drop_table("conversations")
