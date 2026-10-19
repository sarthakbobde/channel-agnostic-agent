"""Baseline: the schema as it was before migrations existed.

Databases created before Alembic already have these tables; Database stamps them at this
revision instead of running it, so existing data is never touched.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meta",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", sa.String(32), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "secrets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_type", "name"),
    )
    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("password_hash", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(254), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    for table in ("audit_log", "admin_users", "secrets", "channels", "meta"):
        op.drop_table(table)
