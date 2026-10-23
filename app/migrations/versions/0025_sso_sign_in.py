"""Sign in with Microsoft or Google: the account each person is linked to, and a session version.

`sso_microsoft` holds "<tenant id>:<object id>" and `sso_google` the Google subject, both set on the first
single sign-on (matched by email) and used from then on. `session_version` is part of the session cookie,
so raising it signs a person out everywhere (unlinking an account does this).

Revision ID: 0025
Revises: 0024
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("sso_microsoft", sa.String(96), nullable=True))
        batch.add_column(sa.Column("sso_google", sa.String(64), nullable=True))
        batch.add_column(sa.Column("session_version", sa.Integer, nullable=False, server_default="0"))
        batch.create_unique_constraint("uq_users_sso_microsoft", ["sso_microsoft"])
        batch.create_unique_constraint("uq_users_sso_google", ["sso_google"])


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_sso_google", type_="unique")
        batch.drop_constraint("uq_users_sso_microsoft", type_="unique")
        batch.drop_column("session_version")
        batch.drop_column("sso_google")
        batch.drop_column("sso_microsoft")
