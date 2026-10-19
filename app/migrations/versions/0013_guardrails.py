"""Guardrails: one settings row and an event log (which check fired, never the content).

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guardrail_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("max_input_tokens", sa.Integer, nullable=False),
        sa.Column("blocked_reply", sa.Text, nullable=False),
        sa.Column("mask_enabled", sa.Boolean, nullable=False),
        sa.Column("masked_types", sa.JSON, nullable=False),
        sa.Column("mask_names", sa.Boolean, nullable=False),
        sa.Column("banned_topics_enabled", sa.Boolean, nullable=False),
        sa.Column("output_checks_enabled", sa.Boolean, nullable=False),
        sa.Column("output_redaction", sa.Boolean, nullable=False),
        sa.Column("allow_list", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(254), nullable=False),
    )
    op.create_table(
        "guardrail_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check", sa.String(24), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),        # input | output
        sa.Column("action", sa.String(10), nullable=False),          # blocked | masked | redacted | error
        sa.Column("department", sa.String(80), nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
    )
    op.create_index("ix_guardrail_events_ts", "guardrail_events", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_guardrail_events_ts", table_name="guardrail_events")
    op.drop_table("guardrail_events")
    op.drop_table("guardrail_settings")
