"""Global settings: runtime-editable values that used to be .env lines.

Only settings that code uses today are here. Later phases add their own columns
(retention maximum, summarise-after-N, prompt limit, ...) in their own migrations.
The single row is created on first read from the .env values (see app/global_settings.py).

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "global_settings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("top_k", sa.Integer, nullable=False),
        sa.Column("score_threshold", sa.Float, nullable=False),
        sa.Column("recent_turns", sa.Integer, nullable=False),
        sa.Column("chunk_size", sa.Integer, nullable=False),
        sa.Column("chunk_overlap", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(254), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("global_settings")
