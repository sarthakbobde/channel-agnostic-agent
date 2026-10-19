"""LLM providers, model settings, prices, usage records and alerts.

Also adds global_settings.approved_providers (decision D15: no provider type is usable until an
Admin approves it).

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("global_settings", sa.Column("approved_providers", sa.JSON, nullable=False, server_default="[]"))
    op.create_table(
        "llm_providers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("endpoint", sa.String(500)),
        sa.Column("deployment", sa.String(100)),
        sa.Column("api_version", sa.String(32)),
        sa.Column("manual_models", sa.JSON, nullable=False),
        sa.Column("available_models", sa.JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_detail", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "model_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("preferred_provider_id", sa.Integer),
        sa.Column("preferred_model", sa.String(200), nullable=False),
        sa.Column("fallback_provider_id", sa.Integer),
        sa.Column("fallback_model", sa.String(200), nullable=False),
        sa.Column("request_timeout", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(254), nullable=False),
    )
    op.create_table(
        "model_prices",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider_type", sa.String(32), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("input_per_1k", sa.Float, nullable=False),
        sa.Column("output_per_1k", sa.Float, nullable=False),
        sa.Column("updated_on", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider_type", "model"),
    )
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("department", sa.String(64)),
        sa.Column("kb_id", sa.Integer),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("input_tokens", sa.Integer),
        sa.Column("output_tokens", sa.Integer),
        sa.Column("cost", sa.Float),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("error", sa.Text, nullable=False),
    )
    op.create_index("ix_usage_records_ts", "usage_records", ["ts"])
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("resolved", sa.Boolean, nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_alerts_key", "alerts", ["key"])


def downgrade() -> None:
    op.drop_index("ix_alerts_key", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("ix_usage_records_ts", table_name="usage_records")
    for table in ("usage_records", "model_prices", "model_settings", "llm_providers"):
        op.drop_table(table)
    with op.batch_alter_table("global_settings") as batch:
        batch.drop_column("approved_providers")
