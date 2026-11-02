"""Release 2 groundwork: who asked each question, and which answer each model call belongs to.

`messages.author_user_id` / `author_email` record the person who asked (several people can ask in one Slack or
Teams thread, so the conversation owner is not enough). `usage_records.message_id` links a model call to the
answer it served; `purpose` is widened so longer purposes fit on PostgreSQL.

History: Release 2 was built on a branch where this migration was numbered 0026, while main's 0026 to 0028 (the
personal-data engine) were built in parallel. The UAT database was migrated to the branch's 0026. On such a database
Alembic believes main's 0026 already ran, so this migration also creates main's 0026 tables and columns when they are
missing, and adds its own columns only when they are missing. A database that followed main gets the columns as usual.

Revision ID: 0029
Revises: 0028
"""
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _repair_pii_engine() -> None:
    """Main's 0026 (the personal-data engine), when a database stamped with the branch's 0026 never ran it."""
    if sa.inspect(op.get_bind()).has_table("pii_vault"):
        return
    spec = importlib.util.spec_from_file_location("pii_engine_0026", Path(__file__).with_name("0026_pii_engine.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.upgrade()


def upgrade() -> None:
    _repair_pii_engine()
    have = _columns("messages")
    if "author_user_id" not in have:
        with op.batch_alter_table("messages") as batch:
            batch.add_column(sa.Column("author_user_id", sa.Integer, nullable=True))
            batch.add_column(sa.Column("author_email", sa.String(254), nullable=False, server_default=""))
    if "message_id" not in _columns("usage_records"):
        with op.batch_alter_table("usage_records") as batch:
            batch.alter_column("purpose", type_=sa.String(32), existing_type=sa.String(16), existing_nullable=False)
            batch.add_column(sa.Column("message_id", sa.Integer, nullable=True))
            batch.create_index("ix_usage_records_message_id", ["message_id"])


def downgrade() -> None:
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_index("ix_usage_records_message_id")
        batch.drop_column("message_id")
        batch.alter_column("purpose", type_=sa.String(16), existing_type=sa.String(32), existing_nullable=False)
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("author_email")
        batch.drop_column("author_user_id")
