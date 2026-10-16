"""Generalise secrets: (channel_type, name) -> (owner_type, owner_id, name).

Providers (Phase 1) and vector stores (Phase 8) will keep their credentials in the same
encrypted table. Existing rows become owner_type='channel'; ciphertext is copied untouched.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _fix_postgres_sequence(table: str) -> None:
    """Rows were copied with explicit ids, so move the id sequence past them (PostgreSQL only)."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                   f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")


def upgrade() -> None:
    op.create_table(
        "secrets_new",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("owner_type", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_type", "owner_id", "name"),
    )
    op.execute("INSERT INTO secrets_new (id, owner_type, owner_id, name, ciphertext, updated_at) "
               "SELECT id, 'channel', channel_type, name, ciphertext, updated_at FROM secrets")
    op.drop_table("secrets")
    op.rename_table("secrets_new", "secrets")
    _fix_postgres_sequence("secrets")


def downgrade() -> None:
    op.create_table(
        "secrets_old",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("channel_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("channel_type", "name"),
    )
    # only channel secrets fit the old shape; provider/store secrets are dropped on downgrade
    op.execute("INSERT INTO secrets_old (id, channel_type, name, ciphertext, updated_at) "
               "SELECT id, owner_id, name, ciphertext, updated_at FROM secrets WHERE owner_type = 'channel'")
    op.drop_table("secrets")
    op.rename_table("secrets_old", "secrets")
    _fix_postgres_sequence("secrets")
