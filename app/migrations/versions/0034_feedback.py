"""Release 3, Phase 1: feedback on answers (D49, D50).

- `feedbacks`: one open record per rater and answer (the rating, a reason, the masked comment, links only), with its state.
  Deleted with its conversation.
- Global Settings gain the feedback settings (off by default).

Revision ID: 0034
Revises: 0033
"""
import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feedbacks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("answer_id", sa.Integer, nullable=False, index=True),
        sa.Column("conversation_id", sa.String(200), nullable=False, index=True),
        sa.Column("rater_key", sa.String(32), nullable=False, server_default=""),
        sa.Column("user_id", sa.Integer, nullable=True),
        sa.Column("kb_id", sa.Integer, nullable=True, index=True),
        sa.Column("department_id", sa.Integer, nullable=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="other"),
        sa.Column("rating", sa.String(4), nullable=False),
        sa.Column("reason", sa.String(24), nullable=False, server_default=""),
        sa.Column("comment", sa.Text, nullable=False, server_default=""),
        sa.Column("attachments", sa.JSON, nullable=False),
        sa.Column("intent", sa.String(10), nullable=True),
        sa.Column("intent_source", sa.String(10), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="captured", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_feedbacks_rater_answer", "feedbacks", ["rater_key", "answer_id"])
    with op.batch_alter_table("global_settings") as b:
        b.add_column(sa.Column("feedback_enabled", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("feedback_reasons", sa.JSON, nullable=True))
        b.add_column(sa.Column("feedback_comment_max_chars", sa.Integer, nullable=False, server_default="1000"))
        b.add_column(sa.Column("feedback_payload_days", sa.Integer, nullable=False, server_default="14"))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        for c in ("feedback_payload_days", "feedback_comment_max_chars", "feedback_reasons", "feedback_enabled"):
            b.drop_column(c)
    op.drop_index("ix_feedbacks_rater_answer", table_name="feedbacks")
    op.drop_table("feedbacks")
