"""Users, departments and roles.

* admin_users becomes users (name, department, status, last sign-in); existing admins keep their
  rows, password hashes and the role 'admin'.
* departments (+ editors) and a one-time set-password link table.
* A default department called 'General' is created and every existing user is placed in it.
* global_settings.max_retention_days (upper limit for a department's retention).

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("retention_days", sa.Integer, nullable=False),
        sa.Column("banned_topics", sa.Text, nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "department_editors",
        sa.Column("department_id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, primary_key=True),
    )
    op.create_table(
        "set_password_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by", sa.String(254), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_set_password_tokens_user_id", "set_password_tokens", ["user_id"])

    op.rename_table("admin_users", "users")
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("name", sa.String(120), nullable=False, server_default=""))
        batch.add_column(sa.Column("department_id", sa.Integer))
        batch.add_column(sa.Column("status", sa.String(12), nullable=False, server_default="Active"))
        batch.add_column(sa.Column("last_sign_in", sa.DateTime(timezone=True)))
    with op.batch_alter_table("global_settings") as batch:
        batch.add_column(sa.Column("max_retention_days", sa.Integer, nullable=False, server_default="365"))

    # the default department, and everyone who exists today goes in it
    op.execute("INSERT INTO departments (name, retention_days, banned_topics, is_default, updated_at) "
               "VALUES ('General', 90, '', " + ("true" if op.get_bind().dialect.name == "postgresql" else "1")
               + ", CURRENT_TIMESTAMP)")
    op.execute("UPDATE users SET department_id = (SELECT id FROM departments WHERE name = 'General'), "
               "name = CASE WHEN name = '' THEN substr(email, 1, instr(email, '@') - 1) ELSE name END"
               if op.get_bind().dialect.name != "postgresql" else
               "UPDATE users SET department_id = (SELECT id FROM departments WHERE name = 'General'), "
               "name = CASE WHEN name = '' THEN split_part(email, '@', 1) ELSE name END")


def downgrade() -> None:
    with op.batch_alter_table("global_settings") as batch:
        batch.drop_column("max_retention_days")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_sign_in")
        batch.drop_column("status")
        batch.drop_column("department_id")
        batch.drop_column("name")
    op.rename_table("users", "admin_users")
    op.drop_index("ix_set_password_tokens_user_id", table_name="set_password_tokens")
    for table in ("set_password_tokens", "department_editors", "departments"):
        op.drop_table(table)
