"""Admin API for documents inside knowledge bases (upload, replace, re-index, move, delete).
Same access rule as knowledge bases: Admins everywhere, Editors in the departments they edit."""
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.kbs_api import _can
from app.config import settings
from app.knowledge import documents, kbs

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api", tags=["admin"])


class MoveBody(BaseModel):
    kb_id: int


def _kb_or_404(ctx: AdminContext, sess: Session, kb_id: int) -> dict:
    kb = kbs.get_kb(ctx.db, kb_id)
    if kb is None or not _can(ctx, sess, kb["department_id"]):
        raise HTTPException(404, "No such knowledge base.")
    return kb


def _doc_or_404(ctx: AdminContext, sess: Session, doc_id: int) -> dict:
    d = documents.get(ctx.db, doc_id)
    if d is None:
        raise HTTPException(404, "No such document.")
    _kb_or_404(ctx, sess, d["kb_id"])
    return d


async def _read(file: UploadFile) -> bytes:
    """Read an upload, stopping early if it is bigger than the limit."""
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"The file is larger than {settings.max_upload_mb} MB.")
    return data


def _public(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "doc_key"}


@router.get("/kbs/{kb_id}/documents")
def list_documents(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _kb_or_404(ctx, sess, kb_id)
    return {"documents": [_public(d) for d in documents.list_for_kb(ctx.db, kb_id)],
            "max_upload_mb": settings.max_upload_mb,
            "types": sorted(documents.parsers.SUPPORTED)}


@router.post("/kbs/{kb_id}/documents", status_code=201)
async def upload(kb_id: int, file: UploadFile = File(...), sess: Session = Depends(admin_session),
                 ctx: AdminContext = Depends(get_ctx)):
    kb = _kb_or_404(ctx, sess, kb_id)
    data = await _read(file)
    try:
        d = documents.add(ctx.db, kb_id, file.filename or "", data, sess.email)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "document.upload", f"{kb['name']} / {d['name']}", f"{d['size']} bytes")
    return _public(d)


@router.post("/documents/{doc_id}/replace")
async def replace(doc_id: int, file: UploadFile = File(...), sess: Session = Depends(admin_session),
                  ctx: AdminContext = Depends(get_ctx)):
    old = _doc_or_404(ctx, sess, doc_id)
    data = await _read(file)
    try:
        d = documents.replace(ctx.db, doc_id, file.filename or "", data)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "document.replace", d["name"], f"was {old['name']}, {d['size']} bytes")
    return _public(d)


@router.post("/documents/{doc_id}/reindex")
def reindex(doc_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _doc_or_404(ctx, sess, doc_id)
    try:
        d = documents.reindex(ctx.db, doc_id)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "document.reindex", d["name"])
    return _public(d)


@router.post("/kbs/{kb_id}/reindex")
def reindex_kb(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    kb = _kb_or_404(ctx, sess, kb_id)
    try:
        n = documents.reindex_kb(ctx.db, kb_id)
    except LookupError as e:
        raise HTTPException(409, str(e))
    ctx.store.record(sess.email, "kb.reindex", kb["name"], f"{n} documents queued")
    return {"queued": n}


@router.post("/documents/{doc_id}/move")
def move(doc_id: int, body: MoveBody, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    d = _doc_or_404(ctx, sess, doc_id)
    target = _kb_or_404(ctx, sess, body.kb_id)
    try:
        moved = documents.move(ctx.db, doc_id, body.kb_id)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "document.move", d["name"], f"-> {target['name']}")
    return _public(moved)


@router.delete("/documents/{doc_id}")
def delete(doc_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    d = _doc_or_404(ctx, sess, doc_id)
    try:
        documents.delete(ctx.db, doc_id)
    except LookupError as e:
        raise HTTPException(409, str(e))
    ctx.store.record(sess.email, "document.delete", d["name"], f"{d['chunks']} chunks removed")
    return {"ok": True}
