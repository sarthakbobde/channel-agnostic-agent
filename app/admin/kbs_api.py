"""Admin API for knowledge bases. Admins manage all of them; an Editor manages the knowledge bases of the
departments they are listed as an editor of (and can create new ones only in those departments)."""
import logging

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy import select

from app import global_settings
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.db import Department, DepartmentEditor
from app.knowledge import access, chunkers, documents, embeddings, kbs, parsers, storeadapters, versions

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api", tags=["admin"])


class KBIn(BaseModel):
    name: str | None = None
    description: str | None = None
    department_id: int | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None
    chunk_strategy: str | None = None
    embedding: str | None = None
    embedding_config: dict | None = None
    embedding_secrets: dict | None = None
    store: str | None = None
    store_config: dict | None = None
    store_secrets: dict | None = None


def _last_indexed(ctx: AdminContext, kb_id: int):
    from sqlalchemy import func
    from app.db import Document
    with ctx.db.session() as s:
        return s.scalar(select(func.max(Document.indexed_at)).where(Document.kb_id == kb_id))


def _store_options() -> list[dict]:
    out = []
    for n, cls in storeadapters.STORE_TYPES.items():
        out.append({"name": n, "title": cls.title, "warning": cls.warning,
                    "fields": [{"name": f.name, "label": f.label, "kind": f.kind, "secret": f.secret, "required": f.required,
                                "help": f.help, "default": f.default} for f in cls.SPEC]})
    return out


def _embedding_options(ctx: AdminContext) -> list[dict]:
    """Every embedding model with the form to configure it (drawn from its SPEC)."""
    names = []
    try:
        secrets = embeddings._secret_store()
        if secrets is not None:
            from app.providers.store import ProviderStore
            names = [p["name"] for p in ProviderStore(ctx.db, secrets).list_all() if p["type"] in ("openai", "openai_compatible")]
    except Exception:
        log.warning("could not list providers for the embedding form", exc_info=True)
    out = []
    for n, cls in embeddings.REGISTRY.items():
        fields = []
        for f in cls.SPEC:
            item = {"name": f.name, "label": f.label, "kind": f.kind, "secret": f.secret, "required": f.required, "help": f.help, "default": f.default}
            if f.name == "provider":
                item["choices"] = names
            elif f.choices:
                item["choices"] = list(f.choices)
            fields.append(item)
        out.append({"name": n, "title": cls.title, "builtin": cls.builtin, "warning": cls.warning, "fields": fields,
                    "ready": (cls.available() if hasattr(cls, "available") else True),
                    "needs_download": hasattr(cls, "available") and not cls.available(),
                    "prices": cls.PRICES})
    return out


def _my_departments(ctx: AdminContext, sess: Session) -> list[dict]:
    with ctx.db.session() as s:
        q = select(Department.id, Department.name).order_by(Department.name)
        if sess.role != "admin":
            q = q.join(DepartmentEditor, DepartmentEditor.department_id == Department.id).where(
                DepartmentEditor.user_id == sess.user_id)
        return [{"id": i, "name": n} for i, n in s.execute(q).all()]


def _visible(ctx: AdminContext, sess: Session) -> list[dict]:
    allowed = {d["id"] for d in _my_departments(ctx, sess)}
    return [k for k in kbs.list_kbs(ctx.db) if sess.role == "admin" or k["department_id"] in allowed]


def _present(ctx: AdminContext, kb: dict, names: dict) -> dict:
    health = {"ok": True, "message": ""}
    try:
        active_store = kbs.store_for(kb)
        stats = active_store.stats()
        ok_, msg_ = active_store.health()
        health = {"ok": ok_, "message": msg_ if not ok_ else ""}
    except Exception as e:
        log.warning("could not read stats for knowledge base %s", kb["id"], exc_info=True)
        stats = {"chunks": None, "documents": None}
        health = {"ok": False, "message": "The store could not be reached (" + type(e).__name__ + ")."}
    return {**kb, "department": names.get(kb["department_id"], ""), "chunks": stats["chunks"],
            "documents": stats["documents"], "status_counts": documents.counts(ctx.db, kb["id"]), "index": versions.summary(ctx.db, kb), "embedding": kb["embedding"], "embedding_config": kb["embedding_config"],
            "embedding_secrets_set": embeddings.kb_secret_names(ctx.db, kb["id"]),
            "store": kb["store"], "store_config": kb["store_config"],
            "store_secrets_set": embeddings.kb_secret_names(ctx.db, kb["id"], kbs.STORE_SECRET_OWNER),
            "active_store": (versions.active(ctx.db, kb["id"]) or {}).get("store"), "health": health,
            "size_bytes": stats.get("size_bytes"), "last_indexed": _last_indexed(ctx, kb["id"]),
            "active_embedding": (versions.active(ctx.db, kb["id"]) or {}).get("embedding"),
            "bound_channels": kbs.channels_using(ctx.db, kb["id"])}


@router.get("/kbs")
def list_kbs(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    depts = _my_departments(ctx, sess)
    with ctx.db.session() as s:
        names = dict(s.execute(select(Department.id, Department.name)).all())
    gs = global_settings.current(ctx.db)
    return {"kbs": [_present(ctx, k, names) for k in _visible(ctx, sess)],
            "departments": depts, "global": {"chunk_size": gs.chunk_size, "chunk_overlap": gs.chunk_overlap},
            "strategies": [{"name": n, **v} for n, v in chunkers.STRATEGIES.items()],
            "embeddings": _embedding_options(ctx), "stores": _store_options()}


def _can(ctx: AdminContext, sess: Session, department_id: int) -> bool:
    return sess.role == "admin" or department_id in {d["id"] for d in _my_departments(ctx, sess)}


@router.post("/kbs", status_code=201)
def create(body: KBIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    dept = body.department_id
    if dept is None:
        mine = _my_departments(ctx, sess)
        dept = (kbs.default_kb(ctx.db) or {}).get("department_id") if sess.role == "admin" else (mine[0]["id"] if mine else None)
    if dept is None or not _can(ctx, sess, dept):
        raise HTTPException(403, "You can only create knowledge bases in departments you edit.")
    try:
        kb = kbs.create_kb(ctx.db, body.name or "", body.description or "", dept, body.chunk_size, body.chunk_overlap,
                           chunk_strategy=body.chunk_strategy or "recursive")
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "kb.create", kb["name"], f"collection {kb['collection']}")
    with ctx.db.session() as s:
        names = dict(s.execute(select(Department.id, Department.name)).all())
    return _present(ctx, kb, names)


def _checked_embedding(ctx: AdminContext, kb: dict, name: str, config: dict, secrets: dict) -> tuple[str, dict]:
    """Validate and TEST an embedding model; only then are its secrets stored. Returns (name, non-secret settings)."""
    if name not in embeddings.REGISTRY:
        raise HTTPException(422, "Unknown embedding model.")
    cls = embeddings.REGISTRY[name]
    allowed = {f.name for f in cls.SPEC}
    unknown = sorted((set(config) | set(secrets)) - allowed)
    if unknown:
        raise HTTPException(422, "Unknown settings: " + ", ".join(unknown))
    config = {k: v for k, v in config.items() if v not in (None, "")}
    secret_names = {f.name for f in cls.SPEC if f.secret}
    stored = embeddings.load_kb_secrets(ctx.db, kb["id"]) if kb["id"] else {}
    merged_secrets = {**{k: v for k, v in stored.items() if k in secret_names}, **{k: v for k, v in secrets.items() if v}}
    problems = cls.validate(config, merged_secrets)
    if problems:
        raise HTTPException(422, "; ".join(problems))
    if "dimensions" in config:
        try:
            config["dimensions"] = int(config["dimensions"])
        except (TypeError, ValueError):
            raise HTTPException(422, "Vector size must be a whole number.")
    model = embeddings.build(name, config, kb, ctx.db, extra_secrets=merged_secrets)
    model.usage = None                                                  # a connection test is not billed work to record
    ok, message = model.test()
    if not ok:
        raise HTTPException(422, "The embedding model did not work: " + message)
    try:
        embeddings.save_kb_secrets(ctx.db, kb["id"], {k: v for k, v in secrets.items() if k in secret_names})
    except ValueError as e:
        raise HTTPException(503, str(e))
    return name, config


def _merged_store_secrets(ctx: AdminContext, kb_id: int, cls, secrets: dict) -> dict:
    names = {f.name for f in cls.SPEC if f.secret}
    stored = embeddings.load_kb_secrets(ctx.db, kb_id, kbs.STORE_SECRET_OWNER)
    return {**{k: v for k, v in stored.items() if k in names}, **{k: v for k, v in secrets.items() if v and k in names}}


def _checked_store(ctx: AdminContext, kb: dict, name: str, config: dict, secrets: dict) -> tuple[str, dict]:
    if name not in storeadapters.STORE_TYPES:
        raise HTTPException(422, "Unknown vector store.")
    cls = storeadapters.STORE_TYPES[name]
    allowed = {f.name for f in cls.SPEC}
    unknown = sorted((set(config) | set(secrets)) - allowed)
    if unknown:
        raise HTTPException(422, "Unknown settings: " + ", ".join(unknown))
    config = {k: v for k, v in config.items() if v not in (None, "")}
    merged = _merged_store_secrets(ctx, kb["id"], cls, secrets)
    problems = cls.validate(config, merged)
    if problems:
        raise HTTPException(422, "; ".join(problems))
    ok, message = cls.test(config, merged)
    if not ok:
        raise HTTPException(422, "The vector store did not work: " + message)
    try:
        embeddings.save_kb_secrets(ctx.db, kb["id"], {k: v for k, v in secrets.items() if k in {f.name for f in cls.SPEC if f.secret}}, kbs.STORE_SECRET_OWNER)
    except ValueError as e:
        raise HTTPException(503, str(e))
    return name, config


@router.post("/kbs/{kb_id}/store/test")
def test_store(kb_id: int, body: KBIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Try a vector store's connection without saving anything."""
    _kb_for(ctx, sess, kb_id)
    name = body.store or "chroma"
    if name not in storeadapters.STORE_TYPES:
        raise HTTPException(422, "Unknown vector store.")
    cls = storeadapters.STORE_TYPES[name]
    config = {k: v for k, v in (body.store_config or {}).items() if v not in (None, "")}
    merged = _merged_store_secrets(ctx, kb_id, cls, body.store_secrets or {})
    problems = cls.validate(config, merged)
    if problems:
        return {"ok": False, "message": "; ".join(problems)}
    ok, message = cls.test(config, merged)
    return {"ok": ok, "message": message}


@router.post("/kbs/{kb_id}/embedding/test")
def test_embedding(kb_id: int, body: KBIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Try an embedding model without saving anything; reports the vector size."""
    kb = _kb_for(ctx, sess, kb_id)
    name = body.embedding or kb["embedding"]
    if name not in embeddings.REGISTRY:
        raise HTTPException(422, "Unknown embedding model.")
    cls = embeddings.REGISTRY[name]
    config = {k: v for k, v in (body.embedding_config or {}).items() if v not in (None, "")}
    secret_names = {f.name for f in cls.SPEC if f.secret}
    stored = embeddings.load_kb_secrets(ctx.db, kb_id)
    merged = {**{k: v for k, v in stored.items() if k in secret_names}, **{k: v for k, v in (body.embedding_secrets or {}).items() if v}}
    problems = cls.validate(config, merged)
    if problems:
        return {"ok": False, "message": "; ".join(problems)}
    model = embeddings.build(name, config, kb, ctx.db, extra_secrets=merged)
    model.usage = None
    ok, message = model.test()
    return {"ok": ok, "message": message}


@router.post("/kbs/{kb_id}/embedding/download")
async def download_embedding(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Download the local multilingual model (about 129 MB). Admin only; recorded in the audit log."""
    _kb_for(ctx, sess, kb_id)
    if sess.role != "admin":
        raise HTTPException(403, "Only Admins can download models.")
    cls = embeddings.LocalMultilingualEmbeddings
    if cls.available():
        return {"ok": True, "message": "The model is already downloaded."}
    try:
        await run_in_threadpool(cls.download)
    except Exception as e:
        log.warning("model download failed", exc_info=True)
        raise HTTPException(502, f"The download failed ({type(e).__name__}). Check the network and try again.")
    ctx.store.record(sess.email, "embedding.download", "multilingual-e5-small", "intfloat/multilingual-e5-small from huggingface.co")
    return {"ok": True, "message": "Downloaded."}


@router.put("/kbs/{kb_id}")
def update(kb_id: int, body: KBIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    current = kbs.get_kb(ctx.db, kb_id)
    if current is None or not _can(ctx, sess, current["department_id"]):
        raise HTTPException(404, "No such knowledge base.")
    sent = body.model_fields_set
    if body.department_id is not None and not _can(ctx, sess, body.department_id):
        raise HTTPException(403, "You can only move a knowledge base to a department you edit.")
    emb_name = emb_config = None
    if "embedding" in sent and body.embedding:
        emb_name, emb_config = _checked_embedding(ctx, current, body.embedding, body.embedding_config or {}, body.embedding_secrets or {})
    store_name = store_config = None
    if "store" in sent and body.store:
        store_name, store_config = _checked_store(ctx, current, body.store, body.store_config or {}, body.store_secrets or {})
    try:
        before, after = kbs.update_kb(
            ctx.db, kb_id, name=body.name if "name" in sent else None,
            description=body.description if "description" in sent else None, department_id=body.department_id,
            chunk_size=body.chunk_size if "chunk_size" in sent else "keep",
            chunk_overlap=body.chunk_overlap if "chunk_overlap" in sent else "keep",
            chunk_strategy=body.chunk_strategy if "chunk_strategy" in sent else None,
            embedding=emb_name, embedding_config=emb_config, store=store_name, store_config=store_config)
    except ValueError as e:
        raise HTTPException(422, str(e))
    changes = [f"{k}: {before[k]} -> {after[k]}" for k in ("name", "description", "department_id", "chunk_size", "chunk_overlap", "chunk_strategy", "embedding", "store")
               if before[k] != after[k]]
    ctx.store.record(sess.email, "kb.update", after["name"], "; ".join(changes) or "no changes")
    with ctx.db.session() as s:
        names = dict(s.execute(select(Department.id, Department.name)).all())
    out = _present(ctx, after, names)
    if any(before[k] != after[k] for k in ("chunk_size", "chunk_overlap", "chunk_strategy", "embedding", "embedding_config", "store", "store_config")):
        out["note"] = "The index keeps its current settings until you rebuild it."
    return out


@router.delete("/kbs/{kb_id}")
def delete(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    current = kbs.get_kb(ctx.db, kb_id)
    if current is None or not _can(ctx, sess, current["department_id"]):
        raise HTTPException(404, "No such knowledge base.")
    try:
        summary = kbs.delete_kb(ctx.db, kb_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    ctx.store.record(sess.email, "kb.delete", summary["name"],
                     f"{summary['chunks_removed']} chunks and {summary['documents_removed']} documents removed; unbound from: {', '.join(summary['channels_unbound']) or 'no channels'}")
    return {"ok": True, **summary}


# ---- versioned index ------------------------------------------------------------------------------
def _kb_for(ctx: AdminContext, sess: Session, kb_id: int) -> dict:
    kb = kbs.get_kb(ctx.db, kb_id)
    if kb is None or not _can(ctx, sess, kb["department_id"]):
        raise HTTPException(404, "No such knowledge base.")
    return kb


@router.get("/kbs/{kb_id}/index")
def index_state(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    kb = _kb_for(ctx, sess, kb_id)
    return {"versions": versions.list_versions(ctx.db, kb_id), "out_of_date": versions.out_of_date(ctx.db, kb),
            "desired": versions.desired(ctx.db, kb), "building": versions.building(ctx.db, kb_id)}


@router.get("/kbs/{kb_id}/index/estimate")
def index_estimate(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _kb_for(ctx, sess, kb_id)
    return versions.estimate(ctx.db, kb_id)


@router.post("/kbs/{kb_id}/index/rebuild", status_code=202)
def index_rebuild(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _kb_for(ctx, sess, kb_id)
    try:
        return versions.start_rebuild(ctx.db, kb_id, sess.email)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/kbs/{kb_id}/index/cancel")
def index_cancel(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _kb_for(ctx, sess, kb_id)
    if not versions.cancel(ctx.db, kb_id, sess.email):
        raise HTTPException(409, "No rebuild is running.")
    return {"ok": True}


class RollbackBody(BaseModel):
    version_id: int


@router.post("/kbs/{kb_id}/index/rollback")
def index_rollback(kb_id: int, body: RollbackBody, sess: Session = Depends(admin_session),
                   ctx: AdminContext = Depends(get_ctx)):
    _kb_for(ctx, sess, kb_id)
    try:
        return versions.rollback(ctx.db, kb_id, body.version_id, sess.email)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


# ---- who may ask a knowledge base (D17) ----------------------------------------------------------
class KBAccessBody(BaseModel):
    group_ids: list[int] = []


@router.get("/kbs/{kb_id}/access")
def get_query_access(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _kb_for(ctx, sess, kb_id)
    ids = access.kb_query_groups(ctx.db, kb_id)
    return {"restricted": bool(ids), "group_ids": ids}


@router.put("/kbs/{kb_id}/access")
def set_query_access(kb_id: int, body: KBAccessBody, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    kb = _kb_for(ctx, sess, kb_id)
    before = access.kb_query_groups(ctx.db, kb_id)
    try:
        ids = access.set_kb_query_groups(ctx.db, kb_id, body.group_ids)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "kb.access", kb["name"],
                     f"{'everyone' if not before else str(len(before)) + ' group(s)'} -> {'everyone' if not ids else str(len(ids)) + ' group(s)'} may ask")
    return {"restricted": bool(ids), "group_ids": ids}


# ---- chunk preview ----------------------------------------------------------------------------------
PREVIEW_LIMIT, SNIPPET = 80, 600


def _int_or_none(text: str, label: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise HTTPException(422, f"{label} must be a whole number.")


@router.post("/kbs/{kb_id}/chunk-preview")
async def chunk_preview(kb_id: int, file: UploadFile = File(...), strategy: str = Form(""), chunk_size: str = Form(""),
                        chunk_overlap: str = Form(""), sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Run a chunking strategy on a sample file and show the chunks. Nothing is stored or embedded (semantic chunking does
    embed the sentences to find the breaks, but nothing is saved). Uses the same code as real indexing."""
    kb = _kb_for(ctx, sess, kb_id)
    strategy = strategy or kb["chunk_strategy"]
    if strategy not in chunkers.STRATEGIES:
        raise HTTPException(422, "Unknown chunking strategy.")
    size, overlap = chunkers.effective(_int_or_none(chunk_size, "Chunk size") or kb["chunk_size"],
                                       _int_or_none(chunk_overlap, "Chunk overlap") if chunk_overlap.strip() else kb["chunk_overlap"], ctx.db)
    try:
        kbs.check_chunking(size, overlap, ctx.db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    data = await file.read(documents.settings.max_upload_mb * 1024 * 1024 + 1)
    name = documents.clean_name(file.filename or "")
    try:
        documents.check_upload(name, data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        try:
            chunker = chunkers.build(strategy, size, overlap)
            chunks = await run_in_threadpool(chunker.chunk, path)
            notes = []
            if strategy == "by_heading" and not any(b.kind == "heading" for b in await run_in_threadpool(parsers.load_blocks, path)):
                notes.append("This file has no headings, so its pages were used instead.")
        except ValueError as e:
            raise HTTPException(422, str(e))
        except Exception:
            raise HTTPException(422, "This file could not be read.")
    if strategy == "semantic":
        notes.append("Semantic chunking embeds every sentence to find the breaks, so it is slower to build and costs more with a paid embedding model.")
    if not chunks:
        raise HTTPException(422, "No readable text was found in this file.")
    lengths = [len(c.text) for c in chunks]
    return {"strategy": strategy, "chunk_size": size, "chunk_overlap": overlap, "count": len(chunks),
            "average_chars": round(sum(lengths) / len(lengths)), "longest": max(lengths), "notes": notes, "shown": min(len(chunks), PREVIEW_LIMIT),
            "chunks": [{"n": i, "page": c.page or None, "heading_path": c.heading_path, "chars": len(c.text), "text": c.text[:SNIPPET]}
                       for i, c in enumerate(chunks[:PREVIEW_LIMIT])]}
