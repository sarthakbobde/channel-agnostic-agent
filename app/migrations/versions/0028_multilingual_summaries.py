"""Release 2, Phase 2: languages, the message catalogue and document summaries.

- `message_catalog`: system texts per language (English seeded byte-identical by the app, other languages need approval).
- `documents`: the document's language and its summary (display only, never used in retrieval or a prompt).
- `messages.language`: the resolved language of a question, the fallback for the next unsure detection (D19).
- `guardrail_events.language`: language rejections and wrong-language answers are counted per language.
- `answer_traces.language_detail`: detection, fallback, accepted list and any wrong-language retry.
- Global Settings and Guardrail Settings gain the language and summary settings (defaults change nothing).

Revision ID: 0028
Revises: 0027
"""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_catalog",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("key", sa.String(48), nullable=False),
        sa.Column("lang", sa.String(16), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="Draft"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("source", sa.String(16), nullable=False, server_default="person"),
        sa.Column("updated_by", sa.String(254), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key", "lang", name="uq_message_catalog"),
    )
    with op.batch_alter_table("documents") as b:
        b.add_column(sa.Column("language", sa.String(16), nullable=False, server_default=""))
        b.add_column(sa.Column("summary", sa.Text, nullable=False, server_default=""))
        b.add_column(sa.Column("summary_lang", sa.String(16), nullable=False, server_default=""))
        b.add_column(sa.Column("summary_status", sa.String(12), nullable=False, server_default=""))
        b.add_column(sa.Column("summary_updated_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("content_hash", sa.String(64), nullable=False, server_default=""))
        b.add_column(sa.Column("summary_hash", sa.String(64), nullable=False, server_default=""))
    with op.batch_alter_table("messages") as b:
        b.add_column(sa.Column("language", sa.String(16), nullable=False, server_default=""))
    with op.batch_alter_table("guardrail_events") as b:
        b.add_column(sa.Column("language", sa.String(16), nullable=False, server_default=""))
    with op.batch_alter_table("answer_traces") as b:
        b.add_column(sa.Column("language_detail", sa.JSON, nullable=True))
    with op.batch_alter_table("global_settings") as b:
        b.add_column(sa.Column("accepted_languages", sa.JSON, nullable=True))
        b.add_column(sa.Column("document_languages", sa.JSON, nullable=True))
        b.add_column(sa.Column("default_language", sa.String(16), nullable=False, server_default="en"))
        b.add_column(sa.Column("language_confidence", sa.Float, nullable=False, server_default="0.5"))
        b.add_column(sa.Column("catalog_languages", sa.JSON, nullable=True))
        b.add_column(sa.Column("summary_enabled", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("summary_max_input_tokens", sa.Integer, nullable=False, server_default="6000"))
    with op.batch_alter_table("guardrail_settings") as b:
        b.add_column(sa.Column("language_lists", sa.JSON, nullable=True))
        b.add_column(sa.Column("unlisted_language", sa.String(16), nullable=False, server_default="english"))


def downgrade() -> None:
    with op.batch_alter_table("guardrail_settings") as b:
        b.drop_column("unlisted_language")
        b.drop_column("language_lists")
    with op.batch_alter_table("global_settings") as b:
        for c in ("summary_max_input_tokens", "summary_enabled", "catalog_languages", "language_confidence", "default_language",
                  "document_languages", "accepted_languages"):
            b.drop_column(c)
    with op.batch_alter_table("answer_traces") as b:
        b.drop_column("language_detail")
    with op.batch_alter_table("guardrail_events") as b:
        b.drop_column("language")
    with op.batch_alter_table("messages") as b:
        b.drop_column("language")
    with op.batch_alter_table("documents") as b:
        for c in ("summary_hash", "content_hash", "summary_updated_at", "summary_status", "summary_lang", "summary", "language"):
            b.drop_column(c)
    op.drop_table("message_catalog")
