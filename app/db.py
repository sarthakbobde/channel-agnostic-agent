"""Admin database (SQLite by default, PostgreSQL supported).

Holds channel settings, encrypted secrets, admins, global settings and the audit log.
The schema is managed by Alembic migrations (app/migrations); Database runs them on start.
"""
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import (JSON, Boolean, DateTime, Float, Integer, LargeBinary, String, Text, UniqueConstraint,
                        create_engine, event, inspect)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
BASELINE_REVISION = "0001"          # schema as it was before migrations existed


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
    """Encrypted credential. `owner_type` is channel today; providers and stores later.
    The plaintext is never stored."""
    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("owner_type", "owner_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class User(Base):
    """A person. Admins and Editors sign in to the admin panel; Viewers are people the assistant
    answers (they have no password in this release)."""
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(16), default="viewer")        # admin | editor | viewer
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="Active")      # Active | Deactivated
    provider: Mapped[str] = mapped_column(String(16), default="local")     # local | entra
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_sign_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Profile photo (PNG, JPEG or WebP, at most 512 KB). Deferred so listing users never loads the bytes.
    avatar: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True, deferred=True)
    avatar_type: Mapped[str] = mapped_column(String(32), default="")


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=90)
    banned_topics: Mapped[str] = mapped_column(Text, default="")           # one topic per line
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)       # exactly one is true
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class DepartmentEditor(Base):
    """Which Editor-role users may edit which department."""
    __tablename__ = "department_editors"
    department_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class KnowledgeBase(Base):
    """A set of documents with its own vector collection. Chunk settings left empty inherit Global Settings."""
    __tablename__ = "knowledge_bases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    department_id: Mapped[int] = mapped_column(Integer)
    collection: Mapped[str] = mapped_column(String(80), unique=True)
    chunk_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_overlap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_strategy: Mapped[str] = mapped_column(String(16), default="recursive")
    embedding: Mapped[str] = mapped_column(String(40), default="chroma-default")
    embedding_config: Mapped[dict] = mapped_column(JSON, default=dict)     # non-secret settings; secrets are stored encrypted
    store: Mapped[str] = mapped_column(String(20), default="chroma")
    store_config: Mapped[dict] = mapped_column(JSON, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Document(Base):
    """One uploaded file in a knowledge base. `doc_key` is the document id stored on every vector chunk
    (for documents indexed before this table existed it is the original file name)."""
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200))
    doc_key: Mapped[str] = mapped_column(String(240))
    size: Mapped[int] = mapped_column(Integer, default=0)
    visibility: Mapped[str] = mapped_column(String(8), default="everyone")  # everyone | people | groups
    status: Mapped[str] = mapped_column(String(12), default="Pending")      # Pending | Indexing | Indexed | Failed
    error: Mapped[str] = mapped_column(Text, default="")
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[str] = mapped_column(String(254), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("kb_id", "name", name="uq_documents_kb_name"),)


class KBIndexVersion(Base):
    """One built index of a knowledge base. Exactly one per knowledge base is active (the one searched).
    The chunk settings and embedding model are what THIS version was built with."""
    __tablename__ = "kb_index_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer)
    collection: Mapped[str] = mapped_column(String(80))
    chunk_size: Mapped[int] = mapped_column(Integer)
    chunk_overlap: Mapped[int] = mapped_column(Integer)
    chunk_strategy: Mapped[str] = mapped_column(String(16), default="recursive")
    embedding: Mapped[str] = mapped_column(String(40), default="chroma-default")
    embedding_config: Mapped[dict] = mapped_column(JSON, default=dict)
    store: Mapped[str] = mapped_column(String(20), default="chroma")
    store_config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="Building")       # Building | Ready | Failed | Cancelled
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    chunks: Mapped[int] = mapped_column(Integer, default=0)
    documents: Mapped[int] = mapped_column(Integer, default=0)
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(254), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    __table_args__ = (UniqueConstraint("kb_id", "version", name="uq_kb_index_version"),)


class Group(Base):
    """A named set of people used to share documents. `department_id` says which department's Editors may manage it
    (empty = Admins only). Membership is looked up when someone asks a question, never copied into documents."""
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class GroupMember(Base):
    __tablename__ = "group_members"
    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class DocumentAccess(Base):
    """Who a restricted document is visible to: one row per person ('user') or group ('group')."""
    __tablename__ = "document_access"
    document_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    ref_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class KBAccess(Base):
    """Groups allowed to ask a knowledge base questions. No rows = everyone on a channel bound to it."""
    __tablename__ = "kb_access"
    kb_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)


class Prompt(Base):
    """A version of a prompt. Never edited after it is saved; at most one per (scope, department) is Published."""
    __tablename__ = "prompts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))                      # base-answer | base-chat | dept
    department_id: Mapped[int] = mapped_column(Integer, default=0)      # 0 for the base prompts
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="Draft")    # Draft | Published | Archived
    text: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(254), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    published_by: Mapped[str | None] = mapped_column(String(254), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (UniqueConstraint("scope", "department_id", "version", name="uq_prompt_version"),)


class GuardrailSettings(Base):
    """The single row of guardrail settings (the screen's source of truth; read with a 2 second cache)."""
    __tablename__ = "guardrail_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_input_tokens: Mapped[int] = mapped_column(Integer, default=1000)
    blocked_reply: Mapped[str] = mapped_column(Text, default="")
    mask_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    masked_types: Mapped[list] = mapped_column(JSON, default=list)
    mask_names: Mapped[bool] = mapped_column(Boolean, default=False)
    banned_topics_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    output_checks_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    output_redaction: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_list: Mapped[list] = mapped_column(JSON, default=list)
    injection_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    injection_sensitivity: Mapped[str] = mapped_column(String(8), default="medium")
    injection_phrases: Mapped[list] = mapped_column(JSON, default=list)
    toxicity_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    toxicity_sensitivity: Mapped[str] = mapped_column(String(8), default="medium")
    toxicity_words: Mapped[list] = mapped_column(JSON, default=list)
    toxicity_allow: Mapped[list] = mapped_column(JSON, default=list)
    leak_check_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    updated_by: Mapped[str] = mapped_column(String(254), default="system")


class GuardrailEvent(Base):
    """One guardrail action. Records which check fired and where, never the question or the answer."""
    __tablename__ = "guardrail_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    check: Mapped[str] = mapped_column(String(24))
    direction: Mapped[str] = mapped_column(String(8))
    action: Mapped[str] = mapped_column(String(10))
    department: Mapped[str] = mapped_column(String(80), default="")
    count: Mapped[int] = mapped_column(Integer, default=1)


class Conversation(Base):
    """One conversation (a Web UI chat or a channel thread). The id is the conversation id the channel uses.
    `summary` and `summarised_upto` are used by the long-chat summary (part 7b)."""
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), default="other")
    owner_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_email: Mapped[str] = mapped_column(String(254), default="")
    department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    department: Mapped[str] = mapped_column(String(80), default="")
    title: Mapped[str] = mapped_column(String(120), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    summarised_upto: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class Message(Base):
    """One message. The text is the MASKED text from the guardrails. `complete` is false for an answer that never finished."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(200), index=True)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    complete: Mapped[bool] = mapped_column(Boolean, default=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SetPasswordToken(Base):
    """One-time link for choosing a password (new user or reset). Only a hash of the token is stored."""
    __tablename__ = "set_password_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str] = mapped_column(String(254))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Who did what. Never contains secret values."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(254), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class GlobalSettings(Base):
    """Single row (id = 1) of runtime-editable settings. See app/global_settings.py."""
    __tablename__ = "global_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    top_k: Mapped[int] = mapped_column(Integer)
    score_threshold: Mapped[float] = mapped_column(Float)
    recent_turns: Mapped[int] = mapped_column(Integer)
    chunk_size: Mapped[int] = mapped_column(Integer)
    chunk_overlap: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    updated_by: Mapped[str] = mapped_column(String(254), default="system")
    approved_providers: Mapped[list] = mapped_column(JSON, default=list)    # provider types an Admin approved (D15)
    max_retention_days: Mapped[int] = mapped_column(Integer, default=365)   # upper limit for any department
    prompt_max_chars: Mapped[int] = mapped_column(Integer, default=4000)    # longest department prompt
    summarise_after_turns: Mapped[int] = mapped_column(Integer, default=12)  # summarise a chat after this many unsummarised turns
    retention_job_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    export_enabled: Mapped[bool] = mapped_column(Boolean, default=False)    # off until the privacy sign-off (D7)
    chat_rate_per_minute: Mapped[int] = mapped_column(Integer, default=20)  # questions per person per minute; 0 = no limit
    retrieval_strategy: Mapped[str] = mapped_column(String(16), default="similarity")   # similarity | keyword | hybrid | mmr
    hybrid_keyword_weight: Mapped[float] = mapped_column(Float, default=0.5)
    diversity: Mapped[float] = mapped_column(Float, default=0.3)


class JobLock(Base):
    """Lets a scheduled job run in one process at a time (decision D11)."""
    __tablename__ = "job_locks"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    locked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LLMProvider(Base):
    """A connection to a model provider. The API key is in `secrets` (owner_type='provider')."""
    __tablename__ = "llm_providers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(32))                      # immutable after create
    endpoint: Mapped[str | None] = mapped_column(String(500), nullable=True)
    deployment: Mapped[str | None] = mapped_column(String(100), nullable=True)
    api_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manual_models: Mapped[list] = mapped_column(JSON, default=list)    # for endpoints that cannot list models
    available_models: Mapped[list] = mapped_column(JSON, default=list) # filled by a successful test
    status: Mapped[str] = mapped_column(String(16), default="Not tested")   # Connected | Failed | Not tested
    status_detail: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ModelSettings(Base):
    """Single row (id = 1): which model answers, and which one takes over when it fails."""
    __tablename__ = "model_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    preferred_provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preferred_model: Mapped[str] = mapped_column(String(200), default="")
    fallback_provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fallback_model: Mapped[str] = mapped_column(String(200), default="")
    request_timeout: Mapped[int] = mapped_column(Integer, default=60)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    updated_by: Mapped[str] = mapped_column(String(254), default="system")


class ModelPrice(Base):
    """Price per 1,000 tokens, edited by hand (decision D6). No prices are seeded."""
    __tablename__ = "model_prices"
    __table_args__ = (UniqueConstraint("provider_type", "model"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_type: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    input_per_1k: Mapped[float] = mapped_column(Float)
    output_per_1k: Mapped[float] = mapped_column(Float)
    updated_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UsageRecord(Base):
    """One row per model call attempt (successful or failed)."""
    __tablename__ = "usage_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="llm")       # llm | embedding (later)
    purpose: Mapped[str] = mapped_column(String(16), default="answer") # router | answer | summarise
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(Float, nullable=True)   # null when the model has no price
    status: Mapped[str] = mapped_column(String(8), default="ok")       # ok | failed
    error: Mapped[str] = mapped_column(Text, default="")


class Alert(Base):
    """A condition an admin should know about. Repeats of the same `key` update one row."""
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(8), default="warning")  # warning | error
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    count: Mapped[int] = mapped_column(Integer, default=1)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audience: Mapped[str] = mapped_column(String(254), default="")        # '' = Admins only; an email = that person sees it too


_MIGRATION_LOCK = threading.RLock()


class Database:
    """`target` is a SQLite file path, ':memory:', or a SQLAlchemy URL such as
    postgresql+psycopg://user:pass@host/db."""

    def __init__(self, target: str | Path):
        target = str(target)
        if "://" in target:
            url, self.dialect = target, target.split(":", 1)[0].split("+")[0]
            kwargs = {}
        elif target == ":memory:":
            url, self.dialect = "sqlite://", "sqlite"
            kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        else:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            url, self.dialect, kwargs = f"sqlite:///{path}", "sqlite", {}
        self.engine = create_engine(url, future=True, **kwargs)

        if self.dialect == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _pragmas(conn, _):                    # safer concurrent access from several threads
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.migrate()

    def migrate(self, revision: str = "head") -> None:
        """Bring the schema to `revision`. A database created before migrations existed
        (tables present, no alembic_version) is stamped at the baseline first, so its data is kept."""
        from alembic import command
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        with _MIGRATION_LOCK, self.engine.begin() as conn:      # Alembic's environment is process-wide: one migration at a time
            cfg.attributes["connection"] = conn
            tables = inspect(conn)
            if tables.has_table("channels") and not tables.has_table("alembic_version"):
                command.stamp(cfg, BASELINE_REVISION)
            command.upgrade(cfg, revision)

    def downgrade(self, revision: str = "base") -> None:
        from alembic import command
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        with _MIGRATION_LOCK, self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, revision)

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


_db_lock = threading.Lock()


@lru_cache(maxsize=1)
def _open_db() -> Database:
    return Database(settings.database_url or settings.admin_db_path)


def get_db() -> Database:
    """The one shared database. The lock matters on the very first call: two threads must not both create and migrate it."""
    with _db_lock:
        return _open_db()
