"""Admin API for settings overridden per scope. Admins may use any scope; an Editor only their own departments and those
departments' knowledge bases. Channel overrides are Admin only. Every change and reset is audited with old and new values."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app import identity, scoped_settings
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.db import Channel, Department
from app.knowledge import kbs

router = APIRouter(prefix="/admin/api", tags=["admin"])


class ScopedIn(BaseModel):
    values: dict


def _editable(ctx: AdminContext, sess: Session) -> set[int] | None:
    """The departments this person may manage; None means all of them."""
    return None if sess.role == "admin" else set(identity.resolve(sess.email, ctx.db).edits_departments)


def _owner(ctx: AdminContext, sess: Session, scope_type: str, scope_id: str) -> int | None:
    """Checks the scope exists and that this person may manage it. Returns the department a knowledge base belongs to."""
    mine = _editable(ctx, sess)
    department = None
    if scope_type == scoped_settings.KB:
        kb = kbs.get_kb(ctx.db, int(scope_id)) if scope_id.isdigit() else None
        if kb is None:
            raise HTTPException(404, "No such knowledge base.")
        department = kb["department_id"]
    elif scope_type == scoped_settings.DEPARTMENT:
        with ctx.db.session() as s:
            if not scope_id.isdigit() or s.get(Department, int(scope_id)) is None:
                raise HTTPException(404, "No such department.")
        department = int(scope_id)
    elif scope_type == scoped_settings.CHANNEL:
        if mine is not None:
            raise HTTPException(403, "Only Admins can change settings for a channel.")
        with ctx.db.session() as s:
            if s.scalar(select(Channel.id).where(Channel.type == scope_id)) is None:
                raise HTTPException(404, "No such channel.")
    else:
        raise HTTPException(422, "Choose a channel, knowledge base or department.")
    if mine is not None and department not in mine:
        raise HTTPException(403, "You can only change settings for your own departments and their knowledge bases.")
    return department


def _view(ctx: AdminContext, scope_type: str, scope_id: str, department: int | None) -> dict:
    return {"scope_type": scope_type, "scope_id": scope_id,
            "fields": scoped_settings.view(scope_type, scope_id, department_id=department, db=ctx.db)}


@router.get("/scoped-settings/scopes")
def scopes(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """What this person may choose on the screen. Channels appear once a setting can be overridden per channel."""
    mine = _editable(ctx, sess)
    with ctx.db.session() as s:
        departments = [{"id": d.id, "name": d.name} for d in s.scalars(select(Department).order_by(Department.name)) if mine is None or d.id in mine]
    has_channel_keys = any(scoped_settings.CHANNEL in k.scopes for k in scoped_settings.REGISTRY.values())
    return {"departments": departments,
            "kbs": [{"id": k["id"], "name": k["name"]} for k in kbs.list_kbs(ctx.db) if mine is None or k["department_id"] in mine],
            "channels": ctx.store.types() if mine is None and has_channel_keys else []}


@router.get("/scoped-settings/{scope_type}/{scope_id}")
def read(scope_type: str, scope_id: str, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return _view(ctx, scope_type, scope_id, _owner(ctx, sess, scope_type, scope_id))


@router.put("/scoped-settings/{scope_type}/{scope_id}")
def write(scope_type: str, scope_id: str, body: ScopedIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    department = _owner(ctx, sess, scope_type, scope_id)
    try:
        changes = scoped_settings.put(scope_type, scope_id, body.values, sess.email, ctx.db)
    except ValueError as e:
        raise HTTPException(422, str(e))
    for key, (before, after) in changes.items():
        ctx.store.record(sess.email, "scoped_setting.update", f"{scope_type}:{scope_id}", f"{key}: {before if before is not None else 'inherited'} -> {after}")
    return _view(ctx, scope_type, scope_id, department)


@router.delete("/scoped-settings/{scope_type}/{scope_id}/{key}")
def reset(scope_type: str, scope_id: str, key: str, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    department = _owner(ctx, sess, scope_type, scope_id)
    if scoped_settings.reset(scope_type, scope_id, key, ctx.db):
        ctx.store.record(sess.email, "scoped_setting.reset", f"{scope_type}:{scope_id}", f"{key}: back to inherited")
    return _view(ctx, scope_type, scope_id, department)
