"""Versioned indexes (rebuild safely, switch when complete, roll back).

Every knowledge base has one ACTIVE index version: the collection that is searched. Changing the chunk
settings (or, later, the embedding model or store) does not touch it. An admin starts a rebuild, which
builds a NEW collection beside the active one; only when it is complete does the knowledge base switch to
it. The previous version is kept for rollback; older ones are deleted (the last two are kept).
Strategies are never mixed in one index: single-document indexing always uses the active version's settings.
"""
import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from app import alerts
from app.db import Database, Document, KBIndexVersion, KnowledgeBase, get_db
from app.knowledge import chunkers, embeddings, kbs

log = logging.getLogger(__name__)
DEFAULT_CHUNKS_PER_SECOND = 20.0          # used for the time estimate until a build has been measured
CHARS_PER_TOKEN = 4
_cancel: set[int] = set()
_lock = threading.Lock()


class RebuildInProgress(LookupError):
    """Raised when a change is attempted while the knowledge base is being rebuilt (maps to HTTP 409)."""


class _Cancelled(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _view(v: KBIndexVersion) -> dict:
    return {"id": v.id, "kb_id": v.kb_id, "version": v.version, "collection": v.collection, "chunk_size": v.chunk_size,
            "chunk_overlap": v.chunk_overlap, "embedding": v.embedding, "store": v.store, "status": v.status,
            "is_active": v.is_active, "chunks": v.chunks, "documents": v.documents, "progress_done": v.progress_done,
            "progress_total": v.progress_total, "error": v.error, "created_by": v.created_by, "created_at": v.created_at,
            "finished_at": v.finished_at, "retired_at": v.retired_at, "seconds": v.seconds}


def _audit(actor: str, action: str, target: str, detail: str = "") -> None:
    try:
        from app.channels import get_store
        store = get_store()
        if store:
            store.record(actor, action, target, detail)
    except Exception:
        log.warning("could not audit %s", action, exc_info=True)


# ---- reading --------------------------------------------------------------------------------------
def list_versions(db: Database, kb_id: int) -> list[dict]:
    with db.session() as s:
        return [_view(v) for v in s.scalars(select(KBIndexVersion).where(KBIndexVersion.kb_id == kb_id)
                                            .order_by(KBIndexVersion.version.desc()))]


def active(db: Database, kb_id: int) -> dict | None:
    with db.session() as s:
        v = s.scalar(select(KBIndexVersion).where(KBIndexVersion.kb_id == kb_id, KBIndexVersion.is_active.is_(True)))
        return _view(v) if v else None


def building(db: Database, kb_id: int) -> dict | None:
    with db.session() as s:
        v = s.scalar(select(KBIndexVersion).where(KBIndexVersion.kb_id == kb_id, KBIndexVersion.status == "Building"))
        return _view(v) if v else None


def rebuilding(db: Database, kb_id: int) -> bool:
    return building(db, kb_id) is not None


def desired(db: Database, kb: dict) -> dict:
    """What a rebuild would build with: the knowledge base's chunk settings (or the global ones) and the embedding model."""
    size, overlap = chunkers.effective(kb.get("chunk_size"), kb.get("chunk_overlap"), db)
    return {"chunk_size": size, "chunk_overlap": overlap, "embedding": embeddings.DEFAULT, "store": "chroma"}


def active_chunker(db: Database, kb: dict) -> chunkers.Chunker:
    """The chunker matching what the active index was built with (falls back to the desired settings)."""
    v = active(db, kb["id"])
    if v is None:
        return chunkers.for_kb(kb, db)
    return chunkers.RecursiveChunker(v["chunk_size"], v["chunk_overlap"])


def out_of_date(db: Database, kb: dict) -> bool:
    v = active(db, kb["id"])
    if v is None:
        return False
    d = desired(db, kb)
    return any(v[k] != d[k] for k in ("chunk_size", "chunk_overlap", "embedding", "store"))


def summary(db: Database, kb: dict) -> dict:
    v, b = active(db, kb["id"]), building(db, kb["id"])
    return {"active_version": v["version"] if v else None, "out_of_date": out_of_date(db, kb),
            "building": ({"version": b["version"], "done": b["progress_done"], "total": b["progress_total"]} if b else None)}


# ---- creating the first version ---------------------------------------------------------------------
def init_for(db: Database, kb: dict, chunks: int | None = None) -> dict:
    """Version 1 of a knowledge base that has none: its current collection, with the settings it uses today."""
    existing = list_versions(db, kb["id"])
    if existing:
        return next((v for v in existing if v["is_active"]), existing[0])
    d = desired(db, kb)
    if chunks is None:
        try:
            chunks = kbs.store_for(kb).count()
        except Exception:
            chunks = 0
    with db.session() as s:
        v = KBIndexVersion(kb_id=kb["id"], version=1, collection=kb["collection"], chunk_size=d["chunk_size"],
                           chunk_overlap=d["chunk_overlap"], embedding=d["embedding"], store=d["store"], status="Ready",
                           is_active=True, chunks=chunks, documents=0, created_by="system", finished_at=_now())
        s.add(v)
        s.flush()
        return _view(v)


def ensure_versions(db: Database | None = None) -> None:
    db = db or get_db()
    for kb in kbs.list_kbs(db):
        init_for(db, kb)


def recover_interrupted(db: Database | None = None) -> int:
    """A restart killed any running build: mark it failed and remove its half-built collection."""
    db = db or get_db()
    with db.session() as s:
        rows = [_view(v) for v in s.scalars(select(KBIndexVersion).where(KBIndexVersion.status == "Building"))]
    for v in rows:
        kbs.store_for({"collection": v["collection"]}).delete()
        _finish(db, v["id"], status="Failed", error="Interrupted by a server restart. Start the rebuild again.")
    return len(rows)


# ---- estimate -----------------------------------------------------------------------------------------
def estimate(db: Database, kb_id: int) -> dict:
    """What a rebuild would cost, from a real parse and chunk of the documents (nothing is embedded or stored)."""
    from app.knowledge import documents
    kb = kbs.get_kb(db, kb_id)
    if kb is None:
        raise ValueError("No such knowledge base.")
    d = desired(db, kb)
    chunker = chunkers.RecursiveChunker(d["chunk_size"], d["chunk_overlap"])
    department = kbs.department_name(db, kb["department_id"])
    chunks = chars = 0
    docs = problems = 0
    for doc in documents.list_for_kb(db, kb_id):
        if doc["status"] != "Indexed":
            continue
        try:
            _, texts, _ = documents.prepare(doc, kb, chunker, department)
        except ValueError:
            problems += 1
            continue
        docs += 1
        chunks += len(texts)
        chars += sum(len(t) for t in texts)
    tokens = chars // CHARS_PER_TOKEN
    model = embeddings.REGISTRY[d["embedding"]]
    last = next((v for v in list_versions(db, kb_id) if v["seconds"] and v["chunks"]), None)
    rate = (last["chunks"] / last["seconds"]) if last else DEFAULT_CHUNKS_PER_SECOND
    price = model.cost_per_million_tokens
    return {"documents": docs, "chunks": chunks, "tokens": tokens, "seconds": round(chunks / rate) if rate else None,
            "cost_usd": None if price is None else round(tokens / 1_000_000 * price, 4),
            "model": model.title, "skipped_documents": problems, "target": d,
            "note": ("Embedding runs on this machine, so there is no charge." if price == 0 else
                     "Cost is not known for this embedding model." if price is None else "Cost is an estimate.")
                    + (" Time is measured from the last build." if last else " Time is a rough guess until a build has been measured.")}


# ---- building -------------------------------------------------------------------------------------------
def start_rebuild(db: Database, kb_id: int, actor: str) -> dict:
    from app.knowledge import documents
    kb = kbs.get_kb(db, kb_id)
    if kb is None:
        raise ValueError("No such knowledge base.")
    if rebuilding(db, kb_id):
        raise RebuildInProgress("A rebuild is already running for this knowledge base.")
    if any(d["status"] in ("Pending", "Indexing") for d in documents.list_for_kb(db, kb_id)):
        raise RebuildInProgress("Some documents are still being indexed. Start the rebuild when they have finished.")
    init_for(db, kb)
    d = desired(db, kb)
    with db.session() as s:
        for old in s.scalars(select(KBIndexVersion).where(KBIndexVersion.kb_id == kb_id,
                                                          KBIndexVersion.status.in_(("Failed", "Cancelled")))):
            s.delete(old)                                       # old failed attempts are just noise
        number = max([n for (n,) in s.execute(select(KBIndexVersion.version).where(KBIndexVersion.kb_id == kb_id))] or [0]) + 1
        collection = f"kb_{kb_id}_v{number}"
        v = KBIndexVersion(kb_id=kb_id, version=number, collection=collection, chunk_size=d["chunk_size"],
                           chunk_overlap=d["chunk_overlap"], embedding=d["embedding"], store=d["store"],
                           status="Building", is_active=False, created_by=actor)
        s.add(v)
        s.flush()
        view = _view(v)
    kbs.store_for({"collection": collection}).delete()           # a leftover collection of the same name must not leak in
    _audit(actor, "kb.rebuild.start", kb["name"], f"version {number}: chunk {d['chunk_size']}/{d['chunk_overlap']}")
    threading.Thread(target=_build, args=(db, view["id"], actor), name=f"rebuild-{kb_id}", daemon=True).start()
    return view


def cancel(db: Database, kb_id: int, actor: str) -> bool:
    b = building(db, kb_id)
    if b is None:
        return False
    with _lock:
        _cancel.add(b["id"])
    _audit(actor, "kb.rebuild.cancel", str(kb_id), f"version {b['version']}")
    return True


def _progress(db: Database, vid: int, **fields) -> None:
    with db.session() as s:
        v = s.get(KBIndexVersion, vid)
        if v is not None:
            for k, val in fields.items():
                setattr(v, k, val)


def _finish(db: Database, vid: int, *, status: str, error: str = "") -> None:
    _progress(db, vid, status=status, error=error[:500], finished_at=_now())


def _build(db: Database, vid: int, actor: str) -> None:
    from app.knowledge import documents
    started = time.time()
    with db.session() as s:
        v = _view(s.get(KBIndexVersion, vid))
    kb = kbs.get_kb(db, v["kb_id"])
    store = kbs.store_for({"collection": v["collection"]})
    try:
        chunker = chunkers.RecursiveChunker(v["chunk_size"], v["chunk_overlap"])
        department = kbs.department_name(db, kb["department_id"])
        docs = [d for d in documents.list_for_kb(db, kb["id"]) if d["status"] == "Indexed"]
        _progress(db, vid, progress_total=len(docs))
        per_doc: dict[int, int] = {}
        total_chunks = 0
        for i, d in enumerate(docs, 1):
            with _lock:
                if vid in _cancel:
                    raise _Cancelled()
            ids, texts, metas = documents.prepare(d, kb, chunker, department)
            documents.add_in_batches(store, ids, texts, metas)
            per_doc[d["id"]] = len(ids)
            total_chunks += len(ids)
            _progress(db, vid, progress_done=i, chunks=total_chunks, documents=i)
        seconds = time.time() - started
        _progress(db, vid, seconds=seconds)
        _activate(db, kb["id"], vid, actor, doc_chunks=per_doc)
        _finish(db, vid, status="Ready")
        alerts.resolve_key(db, f"index_build:{kb['id']}")
        _audit(actor, "kb.rebuild.finish", kb["name"], f"version {v['version']}: {len(docs)} documents, {total_chunks} chunks, {round(seconds)} s")
    except _Cancelled:
        store.delete()
        _finish(db, vid, status="Cancelled", error="Cancelled by an admin. The previous index is still in use.")
    except Exception as e:
        log.warning("rebuild of knowledge base %s failed: %s", kb["id"], e, exc_info=not isinstance(e, ValueError))
        store.delete()
        message = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {str(e)[:300]}"
        _finish(db, vid, status="Failed", error=message)
        try:
            alerts.raise_alert(db, f"index_build:{kb['id']}", "index_build_failed",
                               f"Rebuilding '{kb['name']}' failed: {message} The previous index is still in use.", "error")
        except Exception:
            log.warning("could not raise the rebuild alert", exc_info=True)
        _audit(actor, "kb.rebuild.fail", kb["name"], message)
    finally:
        with _lock:
            _cancel.discard(vid)


# ---- switching and rolling back ---------------------------------------------------------------------------
def _activate(db: Database, kb_id: int, vid: int, actor: str, doc_chunks: dict | None = None) -> dict | None:
    """Make version `vid` the one that is searched. Returns the retired version's row (for reconcile)."""
    with db.session() as s:
        new = s.get(KBIndexVersion, vid)
        kb = s.get(KnowledgeBase, kb_id)
        old = s.scalar(select(KBIndexVersion).where(KBIndexVersion.kb_id == kb_id, KBIndexVersion.is_active.is_(True)))
        if doc_chunks:
            for doc_id, n in doc_chunks.items():
                row = s.get(Document, doc_id)
                if row is not None:
                    row.chunks = n
            s.flush()
        retired = None
        if old is not None and old.id != new.id:
            old.is_active, old.retired_at = False, _now()
            retired = _view(old)
        new.is_active, new.retired_at = True, None
        kb.collection = new.collection
    prune(db, kb_id)
    return retired


def prune(db: Database, kb_id: int) -> list[int]:
    """Keep the active version and the most recent other ready one; delete the rest (and their collections)."""
    removed = []
    with db.session() as s:
        rows = list(s.scalars(select(KBIndexVersion).where(KBIndexVersion.kb_id == kb_id, KBIndexVersion.status == "Ready",
                                                           KBIndexVersion.is_active.is_(False)).order_by(KBIndexVersion.version.desc())))
        doomed = [(r.id, r.collection, r.version) for r in rows[1:]]
        for rid, _, _ in doomed:
            s.delete(s.get(KBIndexVersion, rid))
    for _, collection, version in doomed:
        kbs.store_for({"collection": collection}).delete()
        removed.append(version)
    return removed


def rollback(db: Database, kb_id: int, version_id: int, actor: str) -> dict:
    """Go back to a kept version. Documents added, changed or removed since it was retired are brought up to date."""
    from app.knowledge import documents
    kb = kbs.get_kb(db, kb_id)
    if kb is None:
        raise ValueError("No such knowledge base.")
    if rebuilding(db, kb_id):
        raise RebuildInProgress("A rebuild is running. Cancel it or wait for it to finish.")
    with db.session() as s:
        v = s.get(KBIndexVersion, version_id)
        if v is None or v.kb_id != kb_id or v.status != "Ready":
            raise ValueError("That version is not available.")
        if v.is_active:
            raise ValueError("That version is already the active one.")
        since = v.retired_at
        number = v.version
    _activate(db, kb_id, version_id, actor)
    kb = kbs.get_kb(db, kb_id)
    queued, dropped = _reconcile(db, kb, since)
    _audit(actor, "kb.rollback", kb["name"], f"back to version {number}; {queued} documents re-indexed, {dropped} stale removed")
    return {"version": number, "reindexed": queued, "removed_stale": dropped}


def _reconcile(db: Database, kb: dict, since) -> tuple[int, int]:
    from app.knowledge import documents
    store = kbs.store_for(kb)
    counts: dict[str, int] = {}
    for _, metas in store.iter_metadata():
        for m in metas:
            key = (m or {}).get("doc_id")
            if key:
                counts[key] = counts.get(key, 0) + 1
    docs = documents.list_for_kb(db, kb["id"])
    known = {d["doc_key"] for d in docs}
    dropped = 0
    for key in set(counts) - known:                                   # deleted or moved away since
        store.delete_by_document(key)
        dropped += 1
    queued = 0
    for d in docs:
        if d["status"] != "Indexed":
            continue
        changed = d["doc_key"] not in counts or (since is not None and _after(d["updated_at"], since))
        if changed:
            documents.reindex(db, d["id"])
            queued += 1
        else:
            documents._set(db, d["id"], chunks=counts[d["doc_key"]])
    return queued, dropped


def _after(a, b) -> bool:
    a = a.replace(tzinfo=timezone.utc) if a.tzinfo is None else a
    b = b.replace(tzinfo=timezone.utc) if b.tzinfo is None else b
    return a > b


# ---- removing a knowledge base -------------------------------------------------------------------------------
def purge_kb(db: Database, kb_id: int) -> int:
    """Delete every version's collection and record of a knowledge base."""
    with db.session() as s:
        rows = list(s.scalars(select(KBIndexVersion).where(KBIndexVersion.kb_id == kb_id)))
        names = [r.collection for r in rows]
        for r in rows:
            s.delete(r)
    for name in names:
        kbs.store_for({"collection": name}).delete()
    return len(names)
