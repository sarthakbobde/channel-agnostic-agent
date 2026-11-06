"""Release 3, Phase 1 follow-up (D49, D50, D61, D62).

- `conversations.deleted_at`: a deleted conversation is moved aside, not purged (its rows keep a new id and this timestamp).
- `feedbacks`: one OPEN record per rater and answer (any state but `ticketed`), kept by a partial unique index instead of a plain one.
- Global Settings: `feedback_masking` is added; the unused `feedback_reasons` is dropped (there is no reason list, D50).

Revision ID: 0035
Revises: 0034
"""
import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

OPEN = sa.text("state != 'ticketed'")


def upgrade() -> None:
    with op.batch_alter_table("conversations") as b:
        b.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        b.create_index("ix_conversations_deleted_at", ["deleted_at"])
    op.drop_index("ix_feedbacks_rater_answer", table_name="feedbacks")
    # two open records for one rater and answer cannot exist under the new rule: keep the newest (none exist in a database that never ran Phase 1)
    op.execute(sa.text(
        "DELETE FROM feedbacks WHERE state != 'ticketed' AND id NOT IN "
        "(SELECT MAX(id) FROM feedbacks WHERE state != 'ticketed' GROUP BY rater_key, answer_id)"))
    op.create_index("uq_feedbacks_open", "feedbacks", ["rater_key", "answer_id"], unique=True, sqlite_where=OPEN, postgresql_where=OPEN)
    with op.batch_alter_table("global_settings") as b:
        b.add_column(sa.Column("feedback_masking", sa.String(16), nullable=False, server_default="always"))
        b.drop_column("feedback_reasons")


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as b:
        b.add_column(sa.Column("feedback_reasons", sa.JSON, nullable=True))
        b.drop_column("feedback_masking")
    op.drop_index("uq_feedbacks_open", table_name="feedbacks")
    op.create_index("ix_feedbacks_rater_answer", "feedbacks", ["rater_key", "answer_id"])
    with op.batch_alter_table("conversations") as b:
        b.drop_index("ix_conversations_deleted_at")
        b.drop_column("deleted_at")
