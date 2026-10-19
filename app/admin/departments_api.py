"""Admin API for departments: retention, banned topics and who may edit each one.

Admins manage everything. An Editor sees only the departments they are listed as editor of and may
change only their banned topics and retention (never the name, default flag or editors).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app import global_settings
from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.db import Department, DepartmentEditor, User

router = APIRouter(prefix="/admin/api", tags=["admin"])
MAX_TOPICS, MAX_TOPIC_LEN = 100, 200


class DepartmentIn(BaseModel):
    name: str | None = None
    retention_days: int | None = None
    banned_topics: str | None = None
    is_default: bool | None = None
    editor_ids: list[int] | None = None


def clean_topics(raw: str) -> str:
    """One topic per line; blank lines and repeats (any case) dropped."""
    seen, out = set(), []
    for line in (raw or "").replace("\r", "").split("\n"):
        t = " ".join(line.split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    if len(out) > MAX_TOPICS or any(len(t) > MAX_TOPIC_LEN for t in out):
        raise ValueError(f"Use at most {MAX_TOPICS} topics of {MAX_TOPIC_LEN} characters each.")
    return "\n".join(out)


def _check_retention(days: int, ctx: AdminContext) -> int:
    limit = global_settings.current(ctx.db).max_retention_days
    if isinstance(days, bool) or not 1 <= days <= limit:
        raise ValueError(f"Retention must be between 1 and {limit} days (the global maximum).")
    return days


def _view(s, d: Department) -> dict:
    editors = s.execute(select(User.id, User.email, User.name).join(DepartmentEditor, DepartmentEditor.user_id == User.id)
                        .where(DepartmentEditor.department_id == d.id).order_by(User.email)).all()
    members = s.scalar(select(func.count()).select_from(User).where(User.department_id == d.id)) or 0
    return {"id": d.id, "name": d.name, "retention_days": d.retention_days, "banned_topics": d.banned_topics,
            "is_default": d.is_default, "updated_at": d.updated_at, "members": members,
            "editors": [{"id": i, "email": e, "name": n} for i, e, n in editors]}


def _set_editors(s, dept_id: int, ids: list[int]) -> None:
    ids = sorted(set(ids))
    if ids:
        ok = set(s.scalars(select(User.id).where(User.id.in_(ids), User.role == "editor")))
        if ok != set(ids):
            raise ValueError("Only people with the Editor role can be made editors of a department.")
    s.execute(delete(DepartmentEditor).where(DepartmentEditor.department_id == dept_id))
    for i in ids:
        s.add(DepartmentEditor(department_id=dept_id, user_id=i))


def _own(s, sess: Session) -> set[int]:
    return set(s.scalars(select(DepartmentEditor.department_id).where(DepartmentEditor.user_id == sess.user_id)))


@router.get("/departments")
def list_departments(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        rows = list(s.scalars(select(Department).order_by(Department.name)))
        if sess.role != "admin":
            mine = _own(s, sess)
            rows = [d for d in rows if d.id in mine]
        return {"max_retention_days": global_settings.current(ctx.db).max_retention_days,
                "departments": [_view(s, d) for d in rows]}


@router.post("/departments", status_code=201)
def add_department(body: DepartmentIn, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if sess.role != "admin":
        raise HTTPException(403, "Only Admins can add departments.")
    name = (body.name or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(422, "Give the department a name (up to 80 characters).")
    try:
        days = _check_retention(body.retention_days if body.retention_days is not None else 90, ctx)
        topics = clean_topics(body.banned_topics or "")
        with ctx.db.session() as s:
            if s.scalar(select(Department.id).where(func.lower(Department.name) == name.lower())):
                raise ValueError("A department with that name already exists.")
            d = Department(name=name, retention_days=days, banned_topics=topics, is_default=False)
            s.add(d)
            s.flush()
            _set_editors(s, d.id, body.editor_ids or [])
            s.flush()
            out = _view(s, d)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "department.add", name)
    return out


@router.put("/departments/{did}")
def edit_department(did: int, body: DepartmentIn, sess: Session = Depends(admin_session),
                    ctx: AdminContext = Depends(get_ctx)):
    sent = body.model_fields_set
    try:
        with ctx.db.session() as s:
            d = s.get(Department, did)
            if d is None or (sess.role != "admin" and did not in _own(s, sess)):
                raise HTTPException(404, "No such department.")
            if sess.role != "admin" and sent - {"retention_days", "banned_topics"}:
                raise HTTPException(403, "Editors can change only banned topics and retention.")
            changes = []
            if "name" in sent:
                name = (body.name or "").strip()
                if not name or len(name) > 80:
                    raise ValueError("Give the department a name (up to 80 characters).")
                clash = s.scalar(select(Department.id).where(func.lower(Department.name) == name.lower(), Department.id != did))
                if clash:
                    raise ValueError("A department with that name already exists.")
                if name != d.name:
                    changes.append(f"name: {d.name} -> {name}")
                    d.name = name
            if "retention_days" in sent:
                days = _check_retention(body.retention_days, ctx)
                if days != d.retention_days:
                    changes.append(f"retention: {d.retention_days} -> {days}")
                    d.retention_days = days
            if "banned_topics" in sent:
                topics = clean_topics(body.banned_topics or "")
                if topics != d.banned_topics:
                    changes.append("banned topics changed")
                    d.banned_topics = topics
            if "is_default" in sent:
                if body.is_default is False and d.is_default:
                    raise ValueError("Make another department the default instead.")
                if body.is_default and not d.is_default:
                    s.query(Department).update({Department.is_default: False})
                    d.is_default = True
                    changes.append("made default")
            if "editor_ids" in sent:
                _set_editors(s, did, body.editor_ids or [])
                changes.append("editors changed")
            d.updated_at = datetime.now(timezone.utc)
            s.flush()
            out = _view(s, d)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ctx.store.record(sess.email, "department.update", out["name"], "; ".join(changes) or "no changes")
    return out


@router.delete("/departments/{did}")
def delete_department(did: int, sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    if sess.role != "admin":
        raise HTTPException(403, "Only Admins can delete departments.")
    with ctx.db.session() as s:
        d = s.get(Department, did)
        if d is None:
            raise HTTPException(404, "No such department.")
        if d.is_default:
            raise HTTPException(400, "The default department cannot be deleted.")
        if s.scalar(select(func.count()).select_from(User).where(User.department_id == did)):
            raise HTTPException(400, "Move the people in this department elsewhere first.")
        name = d.name
        s.execute(delete(DepartmentEditor).where(DepartmentEditor.department_id == did))
        s.delete(d)
    ctx.store.record(sess.email, "department.delete", name)
    return {"ok": True}
