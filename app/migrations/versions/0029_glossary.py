"""Release 2, Phase 3: glossaries of acronyms and synonyms, per knowledge base or department, with versions.

- `glossaries`: one per scope (a knowledge base or a department).
- `glossary_versions`: Draft, Published or Archived, like prompt versions.
- `glossary_terms`: the terms of one version.
- `documents.glossary_indexed`: the glossary a document was indexed with (index-time expansion), so a change can flag a re-index.
- Global Settings gain the glossary mode (off by default) and the expansion caps.

Revision ID: 0029
Revises: 0028
"""
import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "glossaries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_id", sa.Integer, nullable=False),
        sa.Column("created_by", sa.String(254), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("scope_type", "scope_id", name="uq_glossaries_scope"),
    )
    op.create_table(
        "glossary_versions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("glossary_id", sa.Integer, nullable=False, index=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="Draft"),
        sa.Column("created_by", sa.String(254), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_by", sa.String(254), nullable=False, server_default=""),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "glossary_terms",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("version_id", sa.Integer, nullable=False, index=True),
        sa.Column("canonical", sa.String(200), nullable=False),
        sa.Column("aliases", sa.JSON, nullable=False),
        sa.Column("kind", sa.String(12), nullable=False, server_default="synonym"),
        sa.Column("language", sa.String(16), nullable=False, server_default=""),
        sa.Column("case_sensitive", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("stable", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("documents") as b:
        b.add_column(sa.Column("glossary_indexed", sa.String(120), nullable=False, server_default=""))
    with op.batch_alter_table("global_settings") as b:
        b.add_column(sa.Column("glossary_mode", sa.String(8), nullable=False, server_default="off"))
        b.add_column(sa.Column("glossary_max_terms", sa.Integer, nullable=False, server_default="5"))
        b.add_column(sa.Column("glossary_max_aliases", sa.Integer, nullable=False, server_default="3"))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        for c in ("glossary_max_aliases", "glossary_max_terms", "glossary_mode"):
            b.drop_column(c)
    with op.batch_alter_table("documents") as b:
        b.drop_column("glossary_indexed")
    op.drop_table("glossary_terms")
    op.drop_table("glossary_versions")
    op.drop_table("glossaries")
