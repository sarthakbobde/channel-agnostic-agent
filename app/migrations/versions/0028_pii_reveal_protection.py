"""Who may see real personal data (12d) and the PII protection switch (12g).

`kb_pii_viewers`: the groups and people who see the real values behind a knowledge base's tokens. Empty by default.
`guardrail_settings.pii_protection`: the master switch (on). `knowledge_bases.pii_protection`: inherit | on | off.

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
        "kb_pii_viewers",
        sa.Column("kb_id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(8), primary_key=True),          # group | user
        sa.Column("ref_id", sa.Integer, primary_key=True),
    )
    with op.batch_alter_table("guardrail_settings") as batch:
        batch.add_column(sa.Column("pii_protection", sa.Boolean, nullable=False, server_default=sa.true()))
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.add_column(sa.Column("pii_protection", sa.String(8), nullable=False, server_default="inherit"))


def downgrade() -> None:
    with op.batch_alter_table("knowledge_bases") as batch:
        batch.drop_column("pii_protection")
    with op.batch_alter_table("guardrail_settings") as batch:
        batch.drop_column("pii_protection")
    op.drop_table("kb_pii_viewers")
