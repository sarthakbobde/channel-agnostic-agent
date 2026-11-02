"""Release 2, Phase 4: suggested follow-up questions (D26, D40, D47).

- `suggestions`: every generated candidate, masked, with its outcome, support score, and when it was shown and clicked.
  Deleted with its conversation.
- Global Settings gain the suggestion settings (off by default).

Revision ID: 0033
Revises: 0032
"""
import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suggestions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("answer_id", sa.Integer, nullable=False, index=True),
        sa.Column("conversation_id", sa.String(200), nullable=False, index=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="other"),
        sa.Column("kb_id", sa.Integer, nullable=True),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("support", sa.Float, nullable=True),
        sa.Column("asker", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shown_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicked_question_id", sa.Integer, nullable=True),
    )
    with op.batch_alter_table("global_settings") as b:
        b.add_column(sa.Column("suggestions_enabled", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("suggestions_requested", sa.Integer, nullable=False, server_default="3"))
        b.add_column(sa.Column("suggestions_shown", sa.Integer, nullable=False, server_default="2"))
        b.add_column(sa.Column("suggestions_dedup_threshold", sa.Float, nullable=False, server_default="0.9"))
        b.add_column(sa.Column("suggestions_mode", sa.String(12), nullable=False, server_default="same_call"))
        b.add_column(sa.Column("suggestions_max_searches", sa.Integer, nullable=False, server_default="3"))
        b.add_column(sa.Column("suggestions_payload_days", sa.Integer, nullable=False, server_default="14"))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        for c in ("suggestions_payload_days", "suggestions_max_searches", "suggestions_mode", "suggestions_dedup_threshold",
                  "suggestions_shown", "suggestions_requested", "suggestions_enabled"):
            b.drop_column(c)
    op.drop_table("suggestions")
