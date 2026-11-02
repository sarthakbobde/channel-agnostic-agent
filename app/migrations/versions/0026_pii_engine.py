"""Personal-data engine: Admin-defined types, a sensitive-keyword list, a detection threshold, and the vault that keeps
the real values behind the tokens used in documents.

`pii_vault` holds one row per (knowledge base, value): the token that replaces it, its type, an HMAC of the value (to
find the same value again) and the value itself, encrypted with the master key. `pii_vault_refs` records which
documents use a token, so a value is removed when the last document that holds it goes.

Revision ID: 0026
Revises: 0025
"""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

SEED_KEYWORDS = '["Salary", "CTC", "DOB", "Date of birth", "Bank account", "Account no", "IFSC", "Aadhaar No", "PAN", ' \
                '"Passport", "Medical", "Diagnosis", "Home address"]'


def upgrade() -> None:
    with op.batch_alter_table("guardrail_settings") as batch:
        batch.add_column(sa.Column("pii_threshold", sa.Integer, nullable=False, server_default="40"))
        batch.add_column(sa.Column("keywords_enabled", sa.Boolean, nullable=False, server_default=sa.true()))
        batch.add_column(sa.Column("sensitive_keywords", sa.JSON, nullable=False, server_default=sa.text(f"'{SEED_KEYWORDS}'")))
    op.create_table(
        "pii_custom_types",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(40), nullable=False, unique=True),
        sa.Column("pattern", sa.Text, nullable=False),
        sa.Column("context", sa.JSON, nullable=False),
        sa.Column("confidence", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(254), nullable=False),
    )
    op.create_table(
        "pii_vault",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kb_id", sa.Integer, nullable=False),
        sa.Column("token", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(48), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("value_encrypted", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("kb_id", "token", name="uq_pii_vault_token"),
        sa.UniqueConstraint("kb_id", "value_hash", name="uq_pii_vault_value"),
    )
    op.create_table(
        "pii_vault_refs",
        sa.Column("vault_id", sa.Integer, sa.ForeignKey("pii_vault.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("doc_id", sa.Integer, primary_key=True),
    )
    op.create_index("ix_pii_vault_refs_doc_id", "pii_vault_refs", ["doc_id"])


def downgrade() -> None:
    op.drop_index("ix_pii_vault_refs_doc_id", table_name="pii_vault_refs")
    op.drop_table("pii_vault_refs")
    op.drop_table("pii_vault")
    op.drop_table("pii_custom_types")
    with op.batch_alter_table("guardrail_settings") as batch:
        for col in ("sensitive_keywords", "keywords_enabled", "pii_threshold"):
            batch.drop_column(col)
