"""Admin API for groups and the people directory used to pick group members and document viewers.
Admins manage every group; an Editor manages the groups that belong to a department they edit."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app import groups
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.admin.kbs_api import _can, _my_departments
from app.db import User

router = APIRouter(prefix="/admin/api", tags=["admin"])


class GroupIn(BaseModel):
    name: str | None = None
    description: str | None = None
    department_id: int | None = None
    member_ids: list[int] | None = None


def _visible(ctx: AdminContext, sess: Session, g: dict) -> bool:
    return sess.role == "admin" or (g["department_id"] is not None and _can(ctx, sess, g["department_id"]))


def _group_or_404(ctx: AdminContext, sess: Session, gid: int) -> dict:
    g = groups.get(ctx.db, gid)
    if g is None or not _visible(ctx, sess, g):
        raise HTTPException(404, "No such group.")
    return g


@router.get("/directory")
def directory(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    """Active people, for choosing group members and document viewers."""
    with ctx.db.session() as s:
        rows = s.execute(select(User.id, User.name, User.email).where(User.status == "Active").order_by(User.email)).all()
    return [{"id": i, "name": n, "email": e} for i, n, e in rows]


@router.get("/groups")
def list_groups(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return {"groups": [g for g in groups.list_groups(ctx.db) if _visible(ctx, sess, g)],
            "departments": _my_departments(ctx, sess)}


@router.post("/groups", status_code=201)
def create(body: GroupIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if sess.role != "admin" and (body.department_id is None or not _can(ctx, sess, body.department_id)):
        raise HTTPException(403, "You can only create groups for departments you edit.")
    try:
        g = groups.create(ctx.db, body.name or "", body.description or "", body.department_id, body.member_ids or [])
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "group.create", g["name"], f"{len(g['members'])} members")
    return g


@router.put("/groups/{gid}")
def update(gid: int, body: GroupIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _group_or_404(ctx, sess, gid)
    sent = body.model_fields_set
    if "department_id" in sent and sess.role != "admin" and (body.department_id is None or not _can(ctx, sess, body.department_id)):
        raise HTTPException(403, "You can only move a group to a department you edit.")
    try:
        before, after = groups.update(ctx.db, gid, name=body.name if "name" in sent else None,
                                      description=body.description if "description" in sent else None,
                                      department_id=body.department_id if "department_id" in sent else "keep",
                                      member_ids=body.member_ids if "member_ids" in sent else None)
    except ValueError as e:
        raise HTTPException(422, str(e))
    old, new = {m["id"] for m in before["members"]}, {m["id"] for m in after["members"]}
    changes = []
    if before["name"] != after["name"]:
        changes.append(f"name: {before['name']} -> {after['name']}")
    if new - old:
        changes.append("added " + ", ".join(m["email"] for m in after["members"] if m["id"] in new - old))
    if old - new:
        changes.append("removed " + ", ".join(m["email"] for m in before["members"] if m["id"] in old - new))
    ctx.store.record(sess.email, "group.update", after["name"], "; ".join(changes) or "no changes")
    return after


@router.delete("/groups/{gid}")
def delete(gid: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    _group_or_404(ctx, sess, gid)
    try:
        g = groups.delete_group(ctx.db, gid)
    except LookupError as e:
        raise HTTPException(409, str(e))
    ctx.store.record(sess.email, "group.delete", g["name"])
    return {"ok": True}
