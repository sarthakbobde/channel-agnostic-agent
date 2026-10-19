"""Knowledge bases: each owns a vector collection, belongs to a department and may override chunking.

The default knowledge base row (pointing at the existing collection) is created by the application on
start, because the collection name comes from configuration, not from the database.

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("department_id", sa.Integer, nullable=False),
        sa.Column("collection", sa.String(80), nullable=False, unique=True),
        sa.Column("chunk_size", sa.Integer),
        sa.Column("chunk_overlap", sa.Integer),
        sa.Column("is_default", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("knowledge_bases")
