"""Prompts: versioned base prompts (answering, conversation) and department prompts, plus the department
prompt length limit in Global Settings.

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("scope", sa.String(16), nullable=False),            # base-answer | base-chat | dept
        sa.Column("department_id", sa.Integer, nullable=False),        # 0 for the base prompts
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(10), nullable=False),            # Draft | Published | Archived
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("created_by", sa.String(254), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_by", sa.String(254)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("scope", "department_id", "version", name="uq_prompt_version"),
    )
    op.create_index("ix_prompts_scope_dept", "prompts", ["scope", "department_id"])
    with op.batch_alter_table("global_settings") as batch:
        batch.add_column(sa.Column("prompt_max_chars", sa.Integer, nullable=False, server_default="4000"))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as batch:
        batch.drop_column("prompt_max_chars")
    op.drop_index("ix_prompts_scope_dept", table_name="prompts")
    op.drop_table("prompts")
