"""Release 3, Phase 2: the ticket settings (D58, D60, D63).

Revision ID: 0037
Revises: 0036
"""
import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        b.add_column(sa.Column("admin_agent_mode", sa.String(8), nullable=False, server_default="off"))
        b.add_column(sa.Column("ticket_assignment", sa.String(24), nullable=False, server_default="department"))
        b.add_column(sa.Column("ticket_related_window_days", sa.Integer, nullable=False, server_default="0"))
        b.add_column(sa.Column("ticket_retention_days", sa.Integer, nullable=False, server_default="0"))
        b.add_column(sa.Column("ticket_alert_channels", sa.JSON, nullable=True))
        b.add_column(sa.Column("ticket_alert_events", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        for c in ("ticket_alert_events", "ticket_alert_channels", "ticket_retention_days", "ticket_related_window_days", "ticket_assignment", "admin_agent_mode"):
            b.drop_column(c)
