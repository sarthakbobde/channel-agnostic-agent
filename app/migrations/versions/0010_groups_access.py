"""Groups and per-document access (who may see a document).

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("department_id", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, primary_key=True),
    )
    op.create_table(
        "document_access",
        sa.Column("document_id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(8), primary_key=True),          # 'user' or 'group'
        sa.Column("ref_id", sa.Integer, primary_key=True),
    )
    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("visibility", sa.String(8), nullable=False, server_default="everyone"))


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("visibility")
    for table in ("document_access", "group_members", "groups"):
        op.drop_table(table)
