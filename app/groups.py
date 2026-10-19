"""Groups: named sets of people used to share documents. Membership is read when a question is asked."""
from sqlalchemy import delete, func, select

from app.db import Database, Department, Document, DocumentAccess, Group, GroupMember, KBAccess, KnowledgeBase, User


def _members(s, gid: int) -> list[dict]:
    rows = s.execute(select(User.id, User.email, User.name, User.status).join(GroupMember, GroupMember.user_id == User.id)
                     .where(GroupMember.group_id == gid).order_by(User.email)).all()
    return [{"id": i, "email": e, "name": n, "status": st} for i, e, n, st in rows]


def _view(s, g: Group, names: dict) -> dict:
    return {"id": g.id, "name": g.name, "description": g.description, "department_id": g.department_id,
            "department": names.get(g.department_id, ""), "members": _members(s, g.id), "updated_at": g.updated_at}


def list_groups(db: Database) -> list[dict]:
    with db.session() as s:
        names = dict(s.execute(select(Department.id, Department.name)).all())
        return [_view(s, g, names) for g in s.scalars(select(Group).order_by(Group.name))]


def get(db: Database, gid: int) -> dict | None:
    with db.session() as s:
        g = s.get(Group, gid)
        if g is None:
            return None
        return _view(s, g, dict(s.execute(select(Department.id, Department.name)).all()))


def _check_members(s, ids: list[int]) -> list[int]:
    ids = sorted({int(i) for i in ids or ()})
    if ids and set(s.scalars(select(User.id).where(User.id.in_(ids)))) != set(ids):
        raise ValueError("One of the chosen people does not exist.")
    return ids


def _check_name(s, name: str, exclude: int | None = None) -> str:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Give the group a name (up to 80 characters).")
    q = select(Group.id).where(func.lower(Group.name) == name.lower())
    if exclude is not None:
        q = q.where(Group.id != exclude)
    if s.scalar(q):
        raise ValueError("A group with that name already exists.")
    return name


def create(db: Database, name: str, description: str, department_id: int | None, member_ids=()) -> dict:
    with db.session() as s:
        name = _check_name(s, name)
        if len(description or "") > 500:
            raise ValueError("The description can be up to 500 characters.")
        if department_id is not None and s.get(Department, department_id) is None:
            raise ValueError("That department does not exist.")
        ids = _check_members(s, member_ids)
        g = Group(name=name, description=(description or "").strip(), department_id=department_id)
        s.add(g)
        s.flush()
        s.add_all(GroupMember(group_id=g.id, user_id=i) for i in ids)
        s.flush()
        gid = g.id
    return get(db, gid)


def update(db: Database, gid: int, *, name=None, description=None, department_id="keep", member_ids=None) -> tuple[dict, dict]:
    before = get(db, gid)
    if before is None:
        raise ValueError("No such group.")
    with db.session() as s:
        g = s.get(Group, gid)
        if name is not None:
            g.name = _check_name(s, name, gid)
        if description is not None:
            if len(description) > 500:
                raise ValueError("The description can be up to 500 characters.")
            g.description = description.strip()
        if department_id != "keep":
            if department_id is not None and s.get(Department, department_id) is None:
                raise ValueError("That department does not exist.")
            g.department_id = department_id
        if member_ids is not None:
            ids = _check_members(s, member_ids)
            s.execute(delete(GroupMember).where(GroupMember.group_id == gid))
            s.add_all(GroupMember(group_id=gid, user_id=i) for i in ids)
    return before, get(db, gid)


def used_by(db: Database, gid: int) -> list[str]:
    """Names of the documents that are shared with this group."""
    with db.session() as s:
        return list(s.scalars(select(Document.name).join(DocumentAccess, DocumentAccess.document_id == Document.id)
                              .where(DocumentAccess.kind == "group", DocumentAccess.ref_id == gid).order_by(Document.name)))


def delete_group(db: Database, gid: int) -> dict:
    g = get(db, gid)
    if g is None:
        raise ValueError("No such group.")
    with db.session() as s:
        kbs_using = list(s.scalars(select(KnowledgeBase.name).join(KBAccess, KBAccess.kb_id == KnowledgeBase.id).where(KBAccess.group_id == gid)))
    if kbs_using:
        raise LookupError("Knowledge bases are restricted to this group (" + ", ".join(kbs_using[:5])
                          + "). Change who can ask them first, so none of them becomes open to everyone.")
    docs = used_by(db, gid)
    if docs:
        raise LookupError("Documents are still shared with this group (" + ", ".join(docs[:5]) + (", …" if len(docs) > 5 else "")
                          + "). Change their access first, so none of them becomes visible to more people than intended.")
    with db.session() as s:
        s.execute(delete(GroupMember).where(GroupMember.group_id == gid))
        s.delete(s.get(Group, gid))
    return g


def groups_of_user(db: Database, user_id: int) -> list[int]:
    with db.session() as s:
        return sorted(s.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user_id)))
