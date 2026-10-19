"""Rule-based injection, toxicity and prompt-leak checks: their settings.

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    empty = sa.text("'[]'")
    with op.batch_alter_table("guardrail_settings") as batch:
        batch.add_column(sa.Column("injection_enabled", sa.Boolean, nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("injection_sensitivity", sa.String(8), nullable=False, server_default="medium"))
        batch.add_column(sa.Column("injection_phrases", sa.JSON, nullable=False, server_default=empty))
        batch.add_column(sa.Column("toxicity_enabled", sa.Boolean, nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("toxicity_sensitivity", sa.String(8), nullable=False, server_default="medium"))
        batch.add_column(sa.Column("toxicity_words", sa.JSON, nullable=False, server_default=empty))
        batch.add_column(sa.Column("toxicity_allow", sa.JSON, nullable=False, server_default=empty))
        batch.add_column(sa.Column("leak_check_enabled", sa.Boolean, nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table("guardrail_settings") as batch:
        for col in ("leak_check_enabled", "toxicity_allow", "toxicity_words", "toxicity_sensitivity", "toxicity_enabled",
                    "injection_phrases", "injection_sensitivity", "injection_enabled"):
            batch.drop_column(col)
