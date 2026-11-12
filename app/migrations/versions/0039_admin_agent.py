"""Release 3, Phase 4: the admin agent's settings (D57, D60).

Revision ID: 0039
Revises: 0038
"""
import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

INTS = (("admin_agent_interval_minutes", "5"), ("admin_agent_daily_fix_cap", "5"), ("admin_agent_timeout_minutes", "30"),
        ("admin_agent_max_steps", "8"), ("admin_agent_token_ceiling", "8000"), ("admin_agent_run_seconds", "120"))


def upgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        for name, default in INTS:
            b.add_column(sa.Column(name, sa.Integer, nullable=False, server_default=default))
        b.add_column(sa.Column("admin_agent_setting_keys", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        b.drop_column("admin_agent_setting_keys")
        for name, _ in reversed(INTS):
            b.drop_column(name)
