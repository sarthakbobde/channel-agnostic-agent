"""The signed-in person's own profile: where they belong (department, groups, departments they edit) and their photo."""
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy import select

from app.admin.api import AdminContext, Session, admin_session, get_ctx
from app.db import Department, DepartmentEditor, Group, GroupMember, User

router = APIRouter(prefix="/admin/api", tags=["admin"])

MAX_AVATAR = 512 * 1024          # the page crops photos to 256 px before upload, so this is generous
# Raster formats only, recognised by their first bytes; the declared type is ignored. SVG is refused: it can carry script.
MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))
IMAGE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; sandbox"}


def _sniff(data: bytes) -> str | None:
    for sig, kind in MAGIC:
        if data.startswith(sig):
            return kind
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _me(s, sess: Session) -> User:
    u = s.get(User, sess.user_id) if sess.user_id else None
    if u is None:
        u = s.scalar(select(User).where(User.email == sess.email))
    if u is None:
        raise HTTPException(404, "No such user.")
    return u


@router.get("/me/profile")
def my_profile(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        u = _me(s, sess)
        dept = s.get(Department, u.department_id) if u.department_id else None
        groups = s.scalars(select(Group.name).join(GroupMember, GroupMember.group_id == Group.id)
                           .where(GroupMember.user_id == u.id).order_by(Group.name)).all()
        edits = s.scalars(select(Department.name).join(DepartmentEditor, DepartmentEditor.department_id == Department.id)
                          .where(DepartmentEditor.user_id == u.id).order_by(Department.name)).all()
        return {"email": u.email, "name": u.name, "role": u.role, "department": dept.name if dept else None,
                "groups": list(groups), "editor_of": list(edits), "has_avatar": bool(u.avatar_type),
                "created_at": u.created_at, "last_sign_in": u.last_sign_in}


@router.get("/me/avatar")
def my_avatar(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        u = _me(s, sess)
        if not u.avatar_type or not u.avatar:
            raise HTTPException(404, "No photo.")
        return Response(content=u.avatar, media_type=u.avatar_type, headers=IMAGE_HEADERS)


@router.put("/me/avatar")
async def set_my_avatar(file: UploadFile = File(...), sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    data = await file.read(MAX_AVATAR + 1)
    if len(data) > MAX_AVATAR:
        raise HTTPException(413, "The photo is larger than 512 KB.")
    kind = _sniff(data)
    if kind is None:
        raise HTTPException(400, "Use a PNG, JPEG or WebP image.")
    with ctx.db.session() as s:
        u = _me(s, sess)
        u.avatar, u.avatar_type = data, kind
    ctx.store.record(sess.email, "profile.photo", "", "updated")
    return {"ok": True}


@router.delete("/me/avatar")
def remove_my_avatar(sess: Session = Depends(admin_session), ctx: AdminContext = Depends(get_ctx)):
    with ctx.db.session() as s:
        u = _me(s, sess)
        u.avatar, u.avatar_type = None, ""
    ctx.store.record(sess.email, "profile.photo", "", "removed")
    return {"ok": True}
