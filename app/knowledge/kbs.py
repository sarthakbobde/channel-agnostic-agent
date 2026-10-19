"""Knowledge base records and the helpers that turn them into stores and chunkers.

The default knowledge base points at the original collection (`COLLECTION_NAME`, normally `docs`), so
existing documents and citations keep working. Every other knowledge base gets its own collection.
"""
import logging
import uuid

from sqlalchemy import func, select

from app import global_settings
from app.config import settings
from app.db import Channel, Database, Department, KnowledgeBase, get_db
from app.knowledge import chunkers, metadata
from app.knowledge.stores import ChromaStore

log = logging.getLogger(__name__)
DEFAULT_NAME = "Company knowledge"


def _view(kb: KnowledgeBase) -> dict:
    return {"id": kb.id, "name": kb.name, "description": kb.description, "department_id": kb.department_id,
            "collection": kb.collection, "chunk_size": kb.chunk_size, "chunk_overlap": kb.chunk_overlap,
            "chunk_strategy": kb.chunk_strategy or "recursive", "embedding": kb.embedding or "chroma-default",
            "embedding_config": kb.embedding_config or {}, "store": kb.store or "chroma", "store_config": kb.store_config or {},
            "is_default": kb.is_default, "created_at": kb.created_at, "updated_at": kb.updated_at}


# ---- reading ------------------------------------------------------------------------------
def list_kbs(db: Database | None = None) -> list[dict]:
    db = db or get_db()
    with db.session() as s:
        return [_view(k) for k in s.scalars(select(KnowledgeBase).order_by(KnowledgeBase.is_default.desc(), KnowledgeBase.name))]


def get_kb(db: Database, kb_id: int) -> dict | None:
    with db.session() as s:
        kb = s.get(KnowledgeBase, kb_id)
        return _view(kb) if kb else None


def default_kb(db: Database | None = None) -> dict | None:
    db = db or get_db()
    with db.session() as s:
        kb = s.scalar(select(KnowledgeBase).where(KnowledgeBase.is_default.is_(True)))
        return _view(kb) if kb else None


def resolve(db: Database | None, ids) -> list[dict]:
    """The existing knowledge bases for these ids, in order; none given or none found -> the default one."""
    db = db or get_db()
    found = [k for k in (get_kb(db, int(i)) for i in ids or ()) if k]
    if found:
        return found
    d = default_kb(db)
    return [d] if d else []


def department_name(db: Database, department_id: int) -> str:
    with db.session() as s:
        d = s.get(Department, department_id)
        return d.name if d else ""


# ---- stores ----------------------------------------------------------------------------------
STORE_SECRET_OWNER = "kb-store"


def _active_spec(kb: dict):
    """(store type, non-secret settings) of the knowledge base's active index version."""
    from app.knowledge import versions
    try:
        v = versions.active(get_db(), kb["id"]) if kb.get("id") else None
    except Exception:
        v = None
    return ((v.get("store") or "chroma"), (v.get("store_config") or {})) if v else ("chroma", {})


def store_for(kb: dict, embedder="auto", store="auto"):
    """The store of a knowledge base's ACTIVE index. Questions are embedded with the model that index was built with
    (None = Chroma embeds itself with the built-in model). Pass `embedder` / `store=(type, settings)` to open a specific index,
    for example one being built or one that belongs to an older version."""
    from app import retriever          # late import: the retriever depends on this module
    from app.knowledge import embeddings, storeadapters
    if embedder == "auto":
        embedder = embeddings.active_for(kb) if kb.get("id") else None
    stype, sconfig = _active_spec(kb) if store == "auto" else store
    name = kb["collection"]
    if stype != "chroma":
        secrets = embeddings.load_kb_secrets(get_db(), kb["id"], STORE_SECRET_OWNER) if kb.get("id") else {}
        return storeadapters.build_remote(stype, name, sconfig, secrets, embedder or embeddings.get())
    if name == settings.collection:
        getter = lambda: retriever.get_collection()          # noqa: E731  (looked up each call so it can be swapped in tests)
    else:
        getter = lambda: retriever.get_client().get_or_create_collection(name, metadata={"hnsw:space": "cosine"})   # noqa: E731

    def drop():
        try:
            retriever.get_client().delete_collection(name)
        except Exception:
            log.info("collection %s was already gone", name)

    return ChromaStore(getter, drop, embeddings=embedder)


def default_store() -> ChromaStore:
    """The default knowledge base's active index; the original collection if the database cannot say."""
    try:
        kb = default_kb()
    except Exception:
        kb = None
    return store_for(kb or {"collection": settings.collection})


# ---- writing ---------------------------------------------------------------------------------
def check_chunking(size: int | None, overlap: int | None, db: Database | None = None) -> None:
    """Raises ValueError if the overrides (combined with Global Settings) are not usable."""
    for label, v, lo, hi in (("Chunk size", size, 100, 8000), ("Chunk overlap", overlap, 0, 7999)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            raise ValueError(f"{label} must be a whole number between {lo} and {hi}, or left empty to use the global value.")
    s, o = chunkers.effective(size, overlap, db)
    if o >= s:
        raise ValueError(f"Chunk overlap ({o}) must be smaller than the chunk size ({s}).")


def check_strategy(strategy: str) -> None:
    if strategy not in chunkers.STRATEGIES:
        raise ValueError("Choose one of: " + ", ".join(v["title"] for v in chunkers.STRATEGIES.values()) + ".")


def create_kb(db: Database, name: str, description: str, department_id: int, chunk_size: int | None = None,
              chunk_overlap: int | None = None, *, collection: str | None = None, is_default: bool = False,
              chunk_strategy: str = "recursive") -> dict:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Give the knowledge base a name (up to 80 characters).")
    if len(description or "") > 500:
        raise ValueError("The description can be up to 500 characters.")
    check_chunking(chunk_size, chunk_overlap, db)
    check_strategy(chunk_strategy)
    with db.session() as s:
        if s.get(Department, department_id) is None:
            raise ValueError("That department does not exist.")
        if s.scalar(select(KnowledgeBase.id).where(func.lower(KnowledgeBase.name) == name.lower())):
            raise ValueError("A knowledge base with that name already exists.")
        kb = KnowledgeBase(name=name, description=(description or "").strip(), department_id=department_id,
                           collection=collection or f"pending-{uuid.uuid4().hex[:12]}", chunk_size=chunk_size,
                           chunk_overlap=chunk_overlap, chunk_strategy=chunk_strategy, is_default=is_default)
        s.add(kb)
        s.flush()
        if collection is None:
            kb.collection = f"kb_{kb.id}"
        view = _view(kb)
    from app.knowledge import versions
    versions.init_for(db, view, chunks=0 if collection is None else None)     # an existing collection is counted
    return view


def update_kb(db: Database, kb_id: int, *, name=None, description=None, department_id=None,
              chunk_size="keep", chunk_overlap="keep", chunk_strategy=None, embedding=None, embedding_config=None,
              store=None, store_config=None) -> tuple[dict, dict]:
    """Returns (before, after). `chunk_size` / `chunk_overlap`: "keep" leaves as is, None = inherit global."""
    with db.session() as s:
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None:
            raise ValueError("No such knowledge base.")
        before = _view(kb)
        new_size = kb.chunk_size if chunk_size == "keep" else chunk_size
        new_overlap = kb.chunk_overlap if chunk_overlap == "keep" else chunk_overlap
        check_chunking(new_size, new_overlap, db)
        if name is not None:
            name = name.strip()
            if not name or len(name) > 80:
                raise ValueError("Give the knowledge base a name (up to 80 characters).")
            if s.scalar(select(KnowledgeBase.id).where(func.lower(KnowledgeBase.name) == name.lower(), KnowledgeBase.id != kb_id)):
                raise ValueError("A knowledge base with that name already exists.")
            kb.name = name
        if description is not None:
            if len(description) > 500:
                raise ValueError("The description can be up to 500 characters.")
            kb.description = description.strip()
        if department_id is not None and department_id != kb.department_id:
            if s.get(Department, department_id) is None:
                raise ValueError("That department does not exist.")
            kb.department_id = department_id
        kb.chunk_size, kb.chunk_overlap = new_size, new_overlap
        if chunk_strategy is not None:
            check_strategy(chunk_strategy)
            kb.chunk_strategy = chunk_strategy
        if embedding is not None:
            kb.embedding = embedding
            kb.embedding_config = embedding_config or {}
        if store is not None:
            kb.store = store
            kb.store_config = store_config or {}
        s.flush()
        return before, _view(kb)


def channels_using(db: Database, kb_id: int) -> list[str]:
    with db.session() as s:
        return [c.type for c in s.scalars(select(Channel)) if kb_id in (c.config or {}).get("kb_ids", [])]


def delete_kb(db: Database, kb_id: int) -> dict:
    """Remove the vectors, take the knowledge base out of every channel that used it, then the record.
    Refuses the default knowledge base. Returns what was cleaned up (for the audit log)."""
    kb = get_kb(db, kb_id)
    if kb is None:
        raise ValueError("No such knowledge base.")
    if kb["is_default"]:
        raise ValueError("The default knowledge base cannot be deleted.")
    store = store_for(kb)
    try:
        chunks = store.count()
    except Exception:
        chunks = 0
    store.delete()
    from app.knowledge import documents
    docs_removed = documents.purge_kb(db, kb_id)
    from sqlalchemy import delete as _delete
    from app.db import KBAccess
    with db.session() as s:
        s.execute(_delete(KBAccess).where(KBAccess.kb_id == kb_id))
    from app.knowledge import embeddings, versions
    versions.purge_kb(db, kb_id)
    embeddings.clear_kb_secrets(db, kb_id)
    embeddings.clear_kb_secrets(db, kb_id, STORE_SECRET_OWNER)
    unbound = []
    with db.session() as s:
        for c in s.scalars(select(Channel)):
            cfg = dict(c.config or {})
            if kb_id in cfg.get("kb_ids", []):
                cfg["kb_ids"] = [i for i in cfg["kb_ids"] if i != kb_id]
                c.config = cfg
                unbound.append(c.type)
        row = s.get(KnowledgeBase, kb_id)
        if row is not None:
            s.delete(row)
    return {"name": kb["name"], "chunks_removed": chunks, "documents_removed": docs_removed, "channels_unbound": unbound}


# ---- first-run set-up: the default knowledge base and complete metadata on existing chunks --------
def ensure_default(db: Database | None = None) -> dict:
    db = db or get_db()
    existing = default_kb(db)
    if existing:
        return existing
    with db.session() as s:
        dept = s.scalar(select(Department.id).where(Department.is_default.is_(True)))
    if dept is None:
        raise RuntimeError("no default department; run the database migrations first")
    kb = create_kb(db, DEFAULT_NAME, "Documents that were indexed before knowledge bases existed.", dept,
                   collection=settings.collection, is_default=True)
    log.info("created the default knowledge base for collection %r", settings.collection)
    return kb


def backfill_metadata(db: Database | None = None, kb: dict | None = None) -> int:
    """Give every chunk of the knowledge base the full metadata schema (existing chunks only had
    source, doc_id and page). Idempotent; returns how many chunks were updated."""
    db = db or get_db()
    kb = kb or default_kb(db)
    if not kb:
        return 0
    dept = department_name(db, kb["department_id"])
    store = store_for(kb)
    fixes_ids, fixes_meta = [], []
    for ids, metas in store.iter_metadata():
        for cid, m in zip(ids, metas):
            m = m or {}
            if not metadata.missing(m) and m.get("kb_id") == kb["id"]:
                continue
            chunk = int(cid.rsplit("::", 1)[1]) if "::" in cid and cid.rsplit("::", 1)[1].isdigit() else 0
            source = m.get("source") or m.get("doc_id") or ""
            full = metadata.build(source=source, doc_id=m.get("doc_id") or source, page=m.get("page") or 0,
                                  chunk=m.get("chunk", chunk), kb_id=kb["id"], department=dept,
                                  heading_path=m.get("heading_path", ""))
            full["allowed_groups"] = m.get("allowed_groups", "")
            full["allowed_people"] = m.get("allowed_people", "")
            fixes_ids.append(cid)
            fixes_meta.append(full)
    for i in range(0, len(fixes_ids), 500):
        store.replace_metadata(fixes_ids[i:i + 500], fixes_meta[i:i + 500])
    return len(fixes_ids)


def run_startup() -> None:
    """Called when the server starts. Never stops the server."""
    try:
        kb = ensure_default()
        n = backfill_metadata(kb=kb)
        if n:
            log.info("completed metadata on %d existing chunks", n)
        from app.knowledge import documents
        made = documents.import_existing()
        if made:
            log.info("recorded %d documents that were already indexed", made)
        from app.knowledge import versions
        versions.recover_interrupted()
        versions.ensure_versions()
        resumed = documents.resume_unfinished()
        if resumed:
            log.info("re-queued %d unfinished documents", resumed)
    except Exception:
        log.exception("knowledge base set-up failed; the original index still answers questions")
