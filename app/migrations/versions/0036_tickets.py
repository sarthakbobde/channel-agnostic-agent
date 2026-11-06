"""Release 3, Phase 2: tickets (D48, D55, D56, D66, D67).

- `ticket_types` (seeded with `feedback`), `ticket_groups` (empty, unused: grouping is not built), `tickets` and `ticket_events`.
- `feedbacks.ticket_id`: the ticket a feedback ended up in.

Revision ID: 0036
Revises: 0035
"""
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    types = op.create_table(
        "ticket_types",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(32), nullable=False, unique=True),
        sa.Column("label", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "ticket_groups",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type_id", sa.Integer, sa.ForeignKey("ticket_types.id"), nullable=False, index=True),
        sa.Column("ref_id", sa.Integer, nullable=True),
        sa.Column("kb_id", sa.Integer, nullable=True, index=True),
        sa.Column("department_id", sa.Integer, nullable=True, index=True),
        sa.Column("category", sa.String(32), nullable=False, server_default=""),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("evidence", sa.JSON, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, index=True),
        sa.Column("group_id", sa.Integer, sa.ForeignKey("ticket_groups.id"), nullable=True),
        sa.Column("assignee_type", sa.String(12), nullable=False, server_default=""),
        sa.Column("assignee_id", sa.Integer, nullable=True, index=True),
        sa.Column("raised_by", sa.Integer, nullable=True, index=True),
        sa.Column("related_ticket_id", sa.Integer, sa.ForeignKey("tickets.id"), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ticket_id", sa.Integer, sa.ForeignKey("tickets.id"), nullable=False, index=True),
        sa.Column("event", sa.String(24), nullable=False),
        sa.Column("actor_type", sa.String(12), nullable=False, server_default="manual"),
        sa.Column("actor_id", sa.Integer, nullable=True),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        sa.Column("detail", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.bulk_insert(types, [{"name": "feedback", "label": "Feedback on an answer", "created_at": datetime.now(timezone.utc)}])
    with op.batch_alter_table("feedbacks") as b:
        b.add_column(sa.Column("ticket_id", sa.Integer, nullable=True))
        b.create_index("ix_feedbacks_ticket_id", ["ticket_id"])


def downgrade() -> None:
    with op.batch_alter_table("feedbacks") as b:
        b.drop_index("ix_feedbacks_ticket_id")
        b.drop_column("ticket_id")
    op.drop_table("ticket_events")
    op.drop_table("tickets")
    op.drop_table("ticket_groups")
    op.drop_table("ticket_types")
