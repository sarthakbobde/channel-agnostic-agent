"""Release 3, Phase 3: feedback processing: the fields on a feedback and the settings (D51 to D54, D60).

Revision ID: 0038
Revises: 0037
"""
import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

SETTINGS = (
    ("feedback_processing_mode", sa.String(12), "off"), ("feedback_intent_enabled", sa.Boolean, "1"), ("feedback_verify_enabled", sa.Boolean, "1"),
    ("feedback_replay_daily_cap", sa.Integer, "50"), ("feedback_retry_limit", sa.Integer, "3"), ("feedback_review_claim_minutes", sa.Integer, "30"),
    ("feedback_review_age_days", sa.Integer, "7"), ("feedback_agent_max_steps", sa.Integer, "6"), ("feedback_agent_timeout_seconds", sa.Integer, "60"),
    ("feedback_agent_token_ceiling", sa.Integer, "6000"))


def upgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        for name, kind, default in SETTINGS:
            b.add_column(sa.Column(name, kind, nullable=False, server_default=default if kind is not sa.Boolean else sa.true()))
    with op.batch_alter_table("feedbacks") as b:
        b.add_column(sa.Column("summary", sa.Text, nullable=False, server_default=""))
        b.add_column(sa.Column("suggested_type", sa.String(32), nullable=False, server_default=""))
        b.add_column(sa.Column("verification", sa.JSON, nullable=True))
        b.add_column(sa.Column("review_reason", sa.String(24), nullable=False, server_default=""))
        b.add_column(sa.Column("reviewer_id", sa.Integer, nullable=True))
        b.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("review_note", sa.Text, nullable=False, server_default=""))
        b.add_column(sa.Column("claimed_by", sa.String(64), nullable=False, server_default=""))
        b.add_column(sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("attempts", sa.Integer, nullable=False, server_default="0"))
        b.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("last_error", sa.String(64), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("feedbacks") as b:
        for c in ("last_error", "next_attempt_at", "attempts", "claimed_until", "claimed_by", "review_note", "reviewed_at", "reviewer_id",
                  "review_reason", "verification", "suggested_type", "summary"):
            b.drop_column(c)
    with op.batch_alter_table("global_settings") as b:
        for name, _, _ in reversed(SETTINGS):
            b.drop_column(name)
