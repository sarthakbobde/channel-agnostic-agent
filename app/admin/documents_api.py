"""Admin API for documents inside knowledge bases (upload, replace, re-index, move, delete).
Same access rule as knowledge bases: Admins everywhere, Editors in the departments they edit."""
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.kbs_api import _can
from app.config import settings
from app.db import DocumentAccess, Group, User
from app.knowledge import access, documents, kbs

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


def _ids(text: str) -> list[int]:
    try:
        return [int(x) for x in (text or "").split(",") if x.strip()]
    except ValueError:
        raise ValueError("The chosen people or groups are not valid.")


class AccessBody(BaseModel):
    visibility: str
    user_ids: list[int] = []
    group_ids: list[int] = []


def _public(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "doc_key"}


def _access_names(ctx: AdminContext, docs: list[dict]) -> dict[int, list[str]]:
    """Who each restricted document is visible to, by name (groups) or name/email (people), for the document list."""
    kind_for = {"groups": "group", "people": "user"}
    restricted = {d["id"]: kind_for.get(d["visibility"]) for d in docs if d["visibility"] in kind_for}
    if not restricted:
        return {}
    with ctx.db.session() as s:
        rows = s.execute(select(DocumentAccess.document_id, DocumentAccess.kind, DocumentAccess.ref_id)
                         .where(DocumentAccess.document_id.in_(list(restricted)))).all()
        rows = [r for r in rows if restricted[r[0]] == r[1]]          # only the kind the document is set to
        gids = {ref for _, kind, ref in rows if kind == "group"}
        uids = {ref for _, kind, ref in rows if kind == "user"}
        gname = dict(s.execute(select(Group.id, Group.name).where(Group.id.in_(gids))).all()) if gids else {}
        uname = {i: (n or e) for i, n, e in s.execute(select(User.id, User.name, User.email).where(User.id.in_(uids))).all()} if uids else {}
    out: dict[int, list[str]] = {}
    for doc_id, kind, ref in rows:
        name = gname.get(ref) if kind == "group" else uname.get(ref)
        if name:
            out.setdefault(doc_id, []).append(name)
    return {k: sorted(v, key=str.lower) for k, v in out.items()}


@router.get("/kbs/{kb_id}/documents")
def list_documents(kb_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _kb_or_404(ctx, sess, kb_id)
    docs = [_public(d) for d in documents.list_for_kb(ctx.db, kb_id)]
    names = _access_names(ctx, docs)
    for d in docs:
        d["access_names"] = names.get(d["id"], [])
    return {"documents": docs,
            "max_upload_mb": settings.max_upload_mb,
            "types": sorted(documents.parsers.SUPPORTED)}


@router.post("/kbs/{kb_id}/documents", status_code=201)
async def upload(kb_id: int, file: UploadFile = File(...), visibility: str = Form("everyone"),
                 group_ids: str = Form(""), user_ids: str = Form(""), sess: Session = Depends(admin_session),
                 ctx: AdminContext = Depends(get_ctx)):
    kb = _kb_or_404(ctx, sess, kb_id)
    data = await _read(file)
    try:
        choice = {"visibility": visibility, "group_ids": _ids(group_ids), "user_ids": _ids(user_ids)}
        d = documents.add(ctx.db, kb_id, file.filename or "", data, sess.email, choice)
    except LookupError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "document.upload", f"{kb['name']} / {d['name']}",
                     f"{d['size']} bytes, " + access.describe(ctx.db, d["id"]))
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


@router.get("/documents/{doc_id}/access")
def get_access(doc_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _doc_or_404(ctx, sess, doc_id)
    return access.get(ctx.db, doc_id)


@router.put("/documents/{doc_id}/access")
def set_access(doc_id: int, body: AccessBody, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    d = _doc_or_404(ctx, sess, doc_id)
    before = access.describe(ctx.db, doc_id)
    try:
        result = access.set_access(ctx.db, doc_id, body.visibility, body.user_ids, body.group_ids)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "document.access", d["name"], f"{before} -> {access.describe(ctx.db, doc_id)}")
    return result
