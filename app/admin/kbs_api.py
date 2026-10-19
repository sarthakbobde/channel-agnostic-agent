"""Admin API for knowledge bases. Admins manage all of them; an Editor manages the knowledge bases of the
departments they are listed as an editor of (and can create new ones only in those departments)."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app import global_settings
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.db import Department, DepartmentEditor
from app.knowledge import access, documents, embeddings, kbs, versions

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api", tags=["admin"])


class KBIn(BaseModel):
    name: str | None = None
    description: str | None = None
    department_id: int | None = None
    chunk_size: int | None = None
    chunk_overlap: int | None = None


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
    try:
        stats = kbs.store_for(kb).stats()
    except Exception:
        log.warning("could not read stats for knowledge base %s", kb["id"], exc_info=True)
        stats = {"chunks": None, "documents": None}
    return {**kb, "department": names.get(kb["department_id"], ""), "chunks": stats["chunks"],
            "documents": stats["documents"], "status_counts": documents.counts(ctx.db, kb["id"]), "index": versions.summary(ctx.db, kb), "embedding": embeddings.DEFAULT, "store": "chroma",
            "bound_channels": kbs.channels_using(ctx.db, kb["id"])}


@router.get("/kbs")
def list_kbs(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    depts = _my_departments(ctx, sess)
    with ctx.db.session() as s:
        names = dict(s.execute(select(Department.id, Department.name)).all())
    gs = global_settings.current(ctx.db)
    return {"kbs": [_present(ctx, k, names) for k in _visible(ctx, sess)],
            "departments": depts, "global": {"chunk_size": gs.chunk_size, "chunk_overlap": gs.chunk_overlap},
            "embeddings": [{"name": n, "title": c.title} for n, c in embeddings.REGISTRY.items()]}


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
        kb = kbs.create_kb(ctx.db, body.name or "", body.description or "", dept, body.chunk_size, body.chunk_overlap)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "kb.create", kb["name"], f"collection {kb['collection']}")
    with ctx.db.session() as s:
        names = dict(s.execute(select(Department.id, Department.name)).all())
    return _present(ctx, kb, names)


@router.put("/kbs/{kb_id}")
def update(kb_id: int, body: KBIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    current = kbs.get_kb(ctx.db, kb_id)
    if current is None or not _can(ctx, sess, current["department_id"]):
        raise HTTPException(404, "No such knowledge base.")
    sent = body.model_fields_set
    if body.department_id is not None and not _can(ctx, sess, body.department_id):
        raise HTTPException(403, "You can only move a knowledge base to a department you edit.")
    try:
        before, after = kbs.update_kb(
            ctx.db, kb_id, name=body.name if "name" in sent else None,
            description=body.description if "description" in sent else None, department_id=body.department_id,
            chunk_size=body.chunk_size if "chunk_size" in sent else "keep",
            chunk_overlap=body.chunk_overlap if "chunk_overlap" in sent else "keep")
    except ValueError as e:
        raise HTTPException(422, str(e))
    changes = [f"{k}: {before[k]} -> {after[k]}" for k in ("name", "description", "department_id", "chunk_size", "chunk_overlap")
               if before[k] != after[k]]
    ctx.store.record(sess.email, "kb.update", after["name"], "; ".join(changes) or "no changes")
    with ctx.db.session() as s:
        names = dict(s.execute(select(Department.id, Department.name)).all())
    out = _present(ctx, after, names)
    if before["chunk_size"] != after["chunk_size"] or before["chunk_overlap"] != after["chunk_overlap"]:
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
