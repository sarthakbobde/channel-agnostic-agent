"""Admin database (SQLite). Holds channel settings, encrypted secrets, admins and the audit log."""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import settings

SCHEMA_VERSION = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Channel(Base):
    """One row per platform (slack, teams, gchat). `config` holds non-secret settings only."""
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(32), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Secret(Base):
    """Encrypted credential belonging to a channel. The plaintext is never stored."""
    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("channel_type", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="admin")
    provider: Mapped[str] = mapped_column(String(16), default="local")     # local | entra
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    """Who did what. Never contains secret values."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class Database:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", future=True)

        @event.listens_for(self.engine, "connect")
        def _pragmas(conn, _):                       # safer concurrent access from several threads
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        with self.session() as s:
            if s.get(Meta, "schema_version") is None:
                s.add(Meta(key="schema_version", value=str(SCHEMA_VERSION)))

    @contextmanager
    def session(self):
        s = self._sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


@lru_cache(maxsize=1)
def get_db() -> Database:
    return Database(settings.admin_db_path)
