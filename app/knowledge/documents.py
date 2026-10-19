"""Documents in knowledge bases: upload, storage, indexing in the background, replace, move, delete.

Status flow (D9): Pending -> Indexing -> Indexed or Failed. Originals are kept on disk under
`DOCS_DIR/<kb id>/<document id>/<file name>` so any document can be re-indexed later.
Indexing runs on one background worker; documents left Pending/Indexing by a restart are picked up again.
"""
import hashlib
import logging
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.db import Database, Document, KnowledgeBase, get_db
from app.knowledge import chunkers, kbs, metadata, parsers

log = logging.getLogger(__name__)
BATCH = 64
RETRY_WAIT = (1, 20)                  # seconds: exponential backoff between attempts (min, max)
RETRY_ATTEMPTS = 4
_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _view(d: Document) -> dict:
    return {"id": d.id, "kb_id": d.kb_id, "name": d.name, "size": d.size, "visibility": d.visibility, "status": d.status, "error": d.error,
            "chunks": d.chunks, "uploaded_by": d.uploaded_by, "created_at": d.created_at,
            "updated_at": d.updated_at, "indexed_at": d.indexed_at, "doc_key": d.doc_key}


def _not_rebuilding(db: Database, kb_id: int) -> None:
    from app.knowledge import versions
    if versions.rebuilding(db, kb_id):
        raise versions.RebuildInProgress("The index of this knowledge base is being rebuilt. Try again when it has finished.")


# ---- files ---------------------------------------------------------------------------------
def doc_dir(kb_id: int, doc_id: int) -> Path:
    return Path(settings.docs_dir) / str(kb_id) / str(doc_id)


def file_path(d: dict) -> Path:
    return doc_dir(d["kb_id"], d["id"]) / d["name"]


def clean_name(raw: str) -> str:
    name = re.sub(r"[^\w.\- ()]", "_", Path((raw or "").replace("\\", "/")).name).strip(" .")
    if not name:
        raise ValueError("The file has no usable name.")
    return name[:150]


def check_upload(name: str, data: bytes) -> None:
    ext = Path(name).suffix.lower()
    if ext in parsers.OLD_FORMATS:
        raise ValueError(f"Old {ext} files are not supported. Save it as {parsers.OLD_FORMATS[ext]} and upload again.")
    if ext not in parsers.SUPPORTED:
        raise ValueError("Supported files: " + ", ".join(sorted(parsers.SUPPORTED)) + ".")
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ValueError(f"The file is larger than {settings.max_upload_mb} MB.")
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise ValueError("This does not look like a PDF.")
    if ext in (".docx", ".pptx") and not data.startswith(b"PK\x03\x04"):
        raise ValueError(f"This does not look like a {ext} file.")
    if ext in (".md", ".txt") and b"\x00" in data[:4096]:
        raise ValueError("This does not look like a text file.")


# ---- reading ---------------------------------------------------------------------------------
def get(db: Database, doc_id: int) -> dict | None:
    with db.session() as s:
        d = s.get(Document, doc_id)
        return _view(d) if d else None


def list_for_kb(db: Database, kb_id: int) -> list[dict]:
    with db.session() as s:
        return [_view(d) for d in s.scalars(select(Document).where(Document.kb_id == kb_id).order_by(Document.name))]


def counts(db: Database, kb_id: int) -> dict:
    with db.session() as s:
        rows = s.execute(select(Document.status, func.count()).where(Document.kb_id == kb_id).group_by(Document.status)).all()
    return {status: n for status, n in rows}


# ---- writing ---------------------------------------------------------------------------------
def add(db: Database, kb_id: int, filename: str, data: bytes, actor: str, access_choice: dict | None = None) -> dict:
    """Store an uploaded file and queue it for indexing."""
    _not_rebuilding(db, kb_id)
    name = clean_name(filename)
    check_upload(name, data)
    if kbs.get_kb(db, kb_id) is None:
        raise ValueError("No such knowledge base.")
    with db.session() as s:
        if s.scalar(select(Document.id).where(Document.kb_id == kb_id, func.lower(Document.name) == name.lower())):
            raise LookupError(f"'{name}' is already in this knowledge base. Use Replace to upload a new version.")
        d = Document(kb_id=kb_id, name=name, doc_key="pending", size=len(data), status="Pending", uploaded_by=actor)
        s.add(d)
        s.flush()
        d.doc_key = f"doc-{d.id}"
        view = _view(d)
    _write(view, data)
    if access_choice and access_choice.get("visibility", "everyone") != "everyone":
        from app.knowledge import access
        try:
            access.set_access(db, view["id"], access_choice["visibility"], access_choice.get("user_ids"), access_choice.get("group_ids"))
        except ValueError:
            delete(db, view["id"])                  # never leave a document that was meant to be restricted open to everyone
            raise
        view = get(db, view["id"])
    enqueue(view["id"])
    return view


def _write(d: dict, data: bytes) -> None:
    folder = doc_dir(d["kb_id"], d["id"])
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if old.name != d["name"]:
            old.unlink(missing_ok=True)
    (folder / d["name"]).write_bytes(data)


def replace(db: Database, doc_id: int, filename: str, data: bytes) -> dict:
    d = get(db, doc_id)
    if d is None:
        raise ValueError("No such document.")
    _not_rebuilding(db, d["kb_id"])
    name = clean_name(filename)
    check_upload(name, data)
    with db.session() as s:
        clash = s.scalar(select(Document.id).where(Document.kb_id == d["kb_id"], func.lower(Document.name) == name.lower(),
                                                   Document.id != doc_id))
        if clash:
            raise LookupError(f"'{name}' is already in this knowledge base.")
        row = s.get(Document, doc_id)
        row.name, row.size, row.status, row.error = name, len(data), "Pending", ""
        view = _view(row)
    _write(view, data)
    enqueue(doc_id)
    return view


def reindex(db: Database, doc_id: int) -> dict:
    d = get(db, doc_id)
    if d is None:
        raise ValueError("No such document.")
    _not_rebuilding(db, d["kb_id"])
    if not file_path(d).exists():
        raise ValueError("The original file is missing; upload it again with Replace.")
    _set(db, doc_id, status="Pending", error="")
    enqueue(doc_id)
    return get(db, doc_id)


def reindex_kb(db: Database, kb_id: int) -> int:
    _not_rebuilding(db, kb_id)
    n = 0
    for d in list_for_kb(db, kb_id):
        if file_path(d).exists():
            _set(db, d["id"], status="Pending", error="")
            enqueue(d["id"])
            n += 1
    return n


def move(db: Database, doc_id: int, to_kb: int) -> dict:
    d = get(db, doc_id)
    target = kbs.get_kb(db, to_kb)
    if d is None:
        raise ValueError("No such document.")
    if target is None:
        raise ValueError("No such knowledge base.")
    if d["kb_id"] == to_kb:
        return d
    _not_rebuilding(db, d["kb_id"])
    _not_rebuilding(db, to_kb)
    source = kbs.get_kb(db, d["kb_id"])
    with db.session() as s:
        if s.scalar(select(Document.id).where(Document.kb_id == to_kb, func.lower(Document.name) == d["name"].lower())):
            raise LookupError(f"'{d['name']}' is already in the target knowledge base.")
    if source:
        kbs.store_for(source).delete_by_document(d["doc_key"])
    old_folder = doc_dir(d["kb_id"], doc_id)
    new_folder = doc_dir(to_kb, doc_id)
    new_folder.parent.mkdir(parents=True, exist_ok=True)
    if old_folder.exists():
        shutil.move(str(old_folder), str(new_folder))
    _set(db, doc_id, kb_id=to_kb, status="Pending", error="", chunks=0)
    enqueue(doc_id)
    return get(db, doc_id)


def delete(db: Database, doc_id: int) -> dict:
    d = get(db, doc_id)
    if d is None:
        raise ValueError("No such document.")
    _not_rebuilding(db, d["kb_id"])
    kb = kbs.get_kb(db, d["kb_id"])
    if kb:
        kbs.store_for(kb).delete_by_document(d["doc_key"])
    shutil.rmtree(doc_dir(d["kb_id"], doc_id), ignore_errors=True)
    from app.knowledge import access
    access.clear(db, doc_id)
    with db.session() as s:
        row = s.get(Document, doc_id)
        if row is not None:
            s.delete(row)
    return d


def purge_kb(db: Database, kb_id: int) -> int:
    """Remove every document row and stored file of a knowledge base (its vectors go with the collection)."""
    with db.session() as s:
        rows = list(s.scalars(select(Document).where(Document.kb_id == kb_id)))
        from app.knowledge import access
        for r in rows:
            access.clear(db, r.id, session=s)
            s.delete(r)
    shutil.rmtree(Path(settings.docs_dir) / str(kb_id), ignore_errors=True)
    return len(rows)


def _set(db: Database, doc_id: int, **fields) -> None:
    with db.session() as s:
        row = s.get(Document, doc_id)
        if row is not None:
            for k, v in fields.items():
                setattr(row, k, v)


# ---- indexing ----------------------------------------------------------------------------------
def _retryable(exc: BaseException) -> bool:
    return not isinstance(exc, (ValueError, TypeError, KeyError))     # bad input never gets better by retrying


def _add_with_retry(store, ids, texts, metas) -> None:
    @retry(stop=stop_after_attempt(RETRY_ATTEMPTS), wait=wait_exponential(multiplier=RETRY_WAIT[0], max=RETRY_WAIT[1]),
           retry=retry_if_exception(_retryable), reraise=True)
    def go():
        store.add(ids, texts, metas)
    go()


def add_in_batches(store, ids, texts, metas, size: int = BATCH) -> None:
    """Embed and store in batches, retrying a failed batch with backoff (rate limits, brief outages)."""
    for i in range(0, len(ids), size):
        _add_with_retry(store, ids[i:i + size], texts[i:i + size], metas[i:i + size])


def prepare(d: dict, kb: dict, chunker, department: str, access=((), ())) -> tuple[list, list, list]:
    """Parse and chunk one document -> (ids, texts, metadatas). Raises ValueError with a message an admin can act on."""
    path = file_path(d)
    if not path.exists():
        raise ValueError(f"The original of '{d['name']}' is missing; upload it again with Replace.")
    chunks = chunker.chunk(path)
    if not chunks:
        raise ValueError("No readable text was found in this file (is it a scanned image?).")
    ids, texts, metas = [], [], []
    for n, ch in enumerate(chunks):
        ids.append(f"{d['doc_key']}::{n}")
        texts.append(ch.text)
        metas.append(metadata.build(source=d["name"], doc_id=d["doc_key"], page=ch.page, chunk=n, kb_id=kb["id"], department=department,
                                    heading_path=ch.heading_path, allowed_groups=access[0], allowed_people=access[1]))
    return ids, texts, metas


def index_document(db: Database, doc_id: int) -> None:
    """Parse, chunk, embed and store one document. Never raises: a problem becomes status Failed."""
    d = get(db, doc_id)
    if d is None:
        return
    _set(db, doc_id, status="Indexing", error="")
    try:
        kb = kbs.get_kb(db, d["kb_id"])
        if kb is None:
            raise ValueError("The knowledge base no longer exists.")
        from app.knowledge import versions
        chunker = versions.active_chunker(db, kb)          # the ACTIVE index's settings: strategies are never mixed in one index
        department = kbs.department_name(db, kb["department_id"])
        from app.knowledge import access
        ids, texts, metas = prepare(d, kb, chunker, department, access.ids_for(db, doc_id))
        n = len(ids)
        store = kbs.store_for(kb)
        store.delete_by_document(d["doc_key"])
        add_in_batches(store, ids, texts, metas)
        access.sync_chunks(db, d)
        _set(db, doc_id, status="Indexed", error="", chunks=n, indexed_at=_now())
        log.info("indexed document %s (%s): %d chunks", doc_id, d["name"], n)
    except Exception as e:
        log.warning("indexing document %s failed: %s", doc_id, e, exc_info=not isinstance(e, ValueError))
        message = str(e) if isinstance(e, ValueError) else f"{type(e).__name__}: {str(e)[:300]}"
        _set(db, doc_id, status="Failed", error=message[:500])


def enqueue(doc_id: int) -> None:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="indexer")
        ex = _executor
    ex.submit(index_document, get_db(), doc_id)


def shutdown() -> None:
    global _executor
    with _lock:
        ex, _executor = _executor, None
    if ex is not None:
        ex.shutdown(wait=False, cancel_futures=True)


# ---- start-up: documents indexed before this table existed, and unfinished work -----------------
def import_existing(db: Database | None = None) -> int:
    """Create a record (status Indexed) for each document already in the default knowledge base's index,
    and keep a copy of its original from data/docs when it is still there. Idempotent."""
    db = db or get_db()
    kb = kbs.default_kb(db)
    if not kb:
        return 0
    with db.session() as s:
        known = set(s.scalars(select(Document.doc_key).where(Document.kb_id == kb["id"])))
        names = set(s.scalars(select(func.lower(Document.name)).where(Document.kb_id == kb["id"])))
    found: dict[str, dict] = {}
    for _, metas in kbs.store_for(kb).iter_metadata():
        for m in metas:
            key = (m or {}).get("doc_id")
            if key and key not in known:
                f = found.setdefault(key, {"source": (m or {}).get("source") or key, "chunks": 0})
                f["chunks"] += 1
    made = 0
    for key, info in found.items():
        name = clean_name(info["source"])
        if name.lower() in names:
            name = clean_name(f"{Path(name).stem}-{hashlib.sha1(key.encode()).hexdigest()[:6]}{Path(name).suffix}")
        original = Path("data/docs") / key
        with db.session() as s:
            d = Document(kb_id=kb["id"], name=name, doc_key=key, size=original.stat().st_size if original.is_file() else 0,
                         status="Indexed", chunks=info["chunks"], uploaded_by="system (existing index)", indexed_at=_now())
            s.add(d)
            s.flush()
            view = _view(d)
        if original.is_file():
            _write(view, original.read_bytes())
        names.add(name.lower())
        made += 1
    return made


def resume_unfinished(db: Database | None = None) -> int:
    """Documents a restart left Pending or Indexing go back in the queue."""
    db = db or get_db()
    with db.session() as s:
        ids = list(s.scalars(select(Document.id).where(Document.status.in_(("Pending", "Indexing")))))
    for i in ids:
        _set(db, i, status="Pending")
        enqueue(i)
    return len(ids)
