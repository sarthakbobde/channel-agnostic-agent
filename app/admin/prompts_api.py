"""Admin API for prompts. The two base prompts are Admin only. A department's prompt can be edited by an Admin or by an
Editor of that department. Every save, publish and rollback is audited (lengths and versions, not the text)."""
import dataclasses
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import chat_service, identity, prompts
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.kbs_api import _can
from app.knowledge import kbs

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/api", tags=["admin"])


class DraftBody(BaseModel):
    scope: str
    department_id: int = 0
    text: str


class PreviewBody(BaseModel):
    scope: str
    department_id: int = 0
    text: str
    question: str
    kb_ids: list[int] = []


def _allow(ctx: AdminContext, sess: Session, scope: str, department_id: int) -> int:
    """Check the caller may touch this prompt; returns the department id to use (0 for the base prompts)."""
    try:
        dept = prompts.check_scope(scope, department_id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if scope in prompts.BASE_SCOPES:
        if sess.role != "admin":
            raise HTTPException(403, "Only Admins can change the base prompt.")
    elif not _can(ctx, sess, dept):
        raise HTTPException(403, "You can only change the prompt of a department you edit.")
    return dept


def _label(ctx: AdminContext, scope: str, dept: int) -> str:
    return prompts.TITLES[scope] + (f" / {kbs.department_name(ctx.db, dept)}" if scope == prompts.DEPT else "")


@router.get("/prompts")
def overview(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    data = prompts.overview(ctx.db)
    if sess.role != "admin":
        mine = {d for d in (x["department_id"] for x in data["departments"]) if _can(ctx, sess, d)}
        data["base"] = []
        data["departments"] = [d for d in data["departments"] if d["department_id"] in mine]
    return data


@router.get("/prompts/versions")
def versions(scope: str, department_id: int = 0, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    dept = _allow(ctx, sess, scope, department_id)
    return {"versions": prompts.list_versions(ctx.db, scope, dept), "builtin": prompts.builtin(scope)}


@router.post("/prompts/drafts", status_code=201)
def save(body: DraftBody, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    dept = _allow(ctx, sess, body.scope, body.department_id)
    try:
        v = prompts.save_draft(ctx.db, body.scope, dept, body.text, sess.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "prompt.save", _label(ctx, body.scope, dept), f"draft v{v['version']}, {v['chars']} characters")
    return v


def _version_or_404(ctx: AdminContext, sess: Session, version_id: int) -> dict:
    v = prompts.get(ctx.db, version_id)
    if v is None:
        raise HTTPException(404, "No such prompt version.")
    _allow(ctx, sess, v["scope"], v["department_id"])
    return v


@router.post("/prompts/versions/{version_id}/publish")
def publish(version_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    v = _version_or_404(ctx, sess, version_id)
    try:
        live, before = prompts.publish(ctx.db, version_id, sess.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "prompt.publish", _label(ctx, v["scope"], v["department_id"]),
                     f"v{live['version']} is live" + (f", v{before['version']} archived" if before else ""))
    return live


@router.post("/prompts/versions/{version_id}/rollback")
def rollback(version_id: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    v = _version_or_404(ctx, sess, version_id)
    try:
        live, before = prompts.rollback(ctx.db, version_id, sess.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "prompt.rollback", _label(ctx, v["scope"], v["department_id"]),
                     f"back to v{live['version']}" + (f", v{before['version']} archived" if before else ""))
    return live


@router.post("/prompts/preview")
def preview(body: PreviewBody, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Ask a test question with the text in the editor. Nothing is published, nothing is saved to a conversation,
    and the model calls are recorded as previews."""
    dept = _allow(ctx, sess, body.scope, body.department_id)
    if not body.question.strip():
        raise HTTPException(422, "Type a test question.")
    try:
        text = prompts.check_text(body.scope, body.text, ctx.db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    who = identity.resolve(sess.email, ctx.db)
    if body.scope == prompts.DEPT:                                  # answer as someone in that department
        who = dataclasses.replace(who, department_id=dept, department=kbs.department_name(ctx.db, dept))
    token = identity.bind(who)
    try:
        with prompts.overriding(body.scope, dept, text):
            kb_ids = body.kb_ids or None
            tokens, meta = chat_service.chat_stream(body.question, None, kb_ids, preview=True)
            answer = "".join(tokens)
    except Exception:
        log.warning("prompt preview failed", exc_info=True)
        raise HTTPException(502, "The preview could not be answered. Check the model providers and try again.")
    finally:
        identity.unbind(token)
    ctx.store.record(sess.email, "prompt.preview", _label(ctx, body.scope, dept), "test question asked")
    return {"answer": answer, "sources": meta.get("sources", []), "searched": meta.get("searched", False)}
