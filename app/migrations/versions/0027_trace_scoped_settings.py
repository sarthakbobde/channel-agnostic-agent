"""Release 2, Phase 1: overrides of settings per scope, and one trace per answer.

`scoped_settings` holds overrides only; the Global Settings row stays the global layer. `answer_traces` explains one answer
with references (ids, scores, versions, timings), never the rendered prompt or passage text.

Revision ID: 0027
Revises: 0026
"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scoped_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_by", sa.String(254), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope_type", "scope_id", "key", name="uq_scoped_settings"),
    )
    op.create_table(
        "answer_traces",
        sa.Column("answer_id", sa.Integer, primary_key=True, autoincrement=False),
        sa.Column("question_id", sa.Integer, nullable=False),
        sa.Column("conversation_id", sa.String(200), nullable=False),
        sa.Column("complete", sa.Boolean, nullable=False),
        sa.Column("found", sa.Boolean, nullable=False),
        sa.Column("searched", sa.Boolean, nullable=False),
        sa.Column("language", sa.String(16), nullable=False, server_default=""),
        sa.Column("language_confidence", sa.Float, nullable=True),
        sa.Column("standalone_query", sa.Text, nullable=False, server_default=""),
        sa.Column("search_text", sa.Text, nullable=False, server_default=""),
        sa.Column("hits", sa.JSON, nullable=False),
        sa.Column("retrieval", sa.JSON, nullable=False),
        sa.Column("prompt_versions", sa.JSON, nullable=False),
        sa.Column("provider", sa.String(64), nullable=False, server_default=""),
        sa.Column("model", sa.String(200), nullable=False, server_default=""),
        sa.Column("fallback_used", sa.Boolean, nullable=False),
        sa.Column("timings", sa.JSON, nullable=False),
        sa.Column("glossary", sa.JSON, nullable=False),
        sa.Column("config_snapshot_id", sa.String(32), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_answer_traces_conversation_id", "answer_traces", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_answer_traces_conversation_id", table_name="answer_traces")
    op.drop_table("answer_traces")
    op.drop_table("scoped_settings")
