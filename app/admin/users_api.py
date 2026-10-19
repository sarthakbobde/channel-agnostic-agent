"""Admin API for people: add, edit, deactivate, and one-time set-password links. Admin only
(enforced by the default-deny map in roles.py)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.admin import users
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.config import settings

router = APIRouter(prefix="/admin/api", tags=["admin"])


class NewUser(BaseModel):
    email: str
    name: str = ""
    role: str = "viewer"
    department_id: int | None = None


class UserEdit(BaseModel):
    name: str | None = None
    role: str | None = None
    department_id: int | None = None
    status: str | None = None


def _link(ctx: AdminContext, uid: int, actor: str) -> dict:
    """Admins and Editors choose their password on the admin page; Viewers on the Web UI."""
    person = users.get_user_by_id(ctx.db, uid)
    token, expires = users.issue_set_password_link(ctx.db, uid, actor)
    if person["role"] in users.PANEL_ROLES:
        return {"token": token, "expires_at": expires.isoformat(), "target": "admin", "path": "/admin#setpw=" + token}
    return {"token": token, "expires_at": expires.isoformat(), "target": "web",
            "url": settings.web_ui_url.rstrip("/") + "/?setpw=" + token}


@router.get("/users")
def list_users(_: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    return users.list_users(ctx.db)


@router.post("/users", status_code=201)
def add_user(body: NewUser, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    try:
        created = users.create_user(ctx.db, body.email, body.name, body.role, body.department_id, domain=ctx.domain)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(s.email, "user.add", created["email"], f"role {created['role']}")
    out = {"user": users.public(created), "link": None}
    out["link"] = _link(ctx, created["id"], s.email)
    ctx.store.record(s.email, "user.link", created["email"], "set-password link issued")
    return out


@router.put("/users/{uid}")
def edit_user(uid: int, body: UserEdit, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if uid == s.user_id and (body.role not in (None, s.role) or body.status not in (None, "Active")):
        raise HTTPException(400, "You cannot change your own role or deactivate yourself.")
    before = users.get_user_by_id(ctx.db, uid)
    if before is None:
        raise HTTPException(404, "No such person.")
    try:
        after = users.update_user(ctx.db, uid, name=body.name, role=body.role,
                                  department_id=body.department_id, status=body.status)
    except ValueError as e:
        raise HTTPException(422, str(e))
    changes = [f"{k}: {before[k]} -> {after[k]}" for k in ("name", "role", "department_id", "status")
               if before[k] != after[k]]
    ctx.store.record(s.email, "user.update", after["email"], "; ".join(changes) or "no changes")
    return users.public(after)


@router.post("/users/{uid}/reset-link")
def reset_link(uid: int, s: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    person = users.get_user_by_id(ctx.db, uid)
    if person is None:
        raise HTTPException(404, "No such person.")
    try:
        link = _link(ctx, uid, s.email)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(s.email, "user.link", person["email"], "set-password link issued")
    return {"link": link}
