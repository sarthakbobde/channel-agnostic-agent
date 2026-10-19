"""Alembic environment. Migrations run from app.db.Database, which passes in its connection."""
from alembic import context

from app.db import Base

config = context.config
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations through app.db.Database (it supplies the connection).")

context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True,
                  compare_type=True)
with context.begin_transaction():
    context.run_migrations()
