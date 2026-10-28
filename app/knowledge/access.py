"""Who may see a document: Everyone, Selected people or Selected groups.

The rows live in `document_access`; the same ids are written into every chunk's metadata (`allowed_groups`,
`allowed_people`) so a store can filter on them. Changing access updates that metadata IN PLACE with
`update_metadata`: no re-embedding. Group membership and a person's status are NOT copied anywhere: they are
looked up when someone asks (Part 4b), so removing a person from a group takes effect immediately.

A restricted document always has at least one person or group, so it can never silently become visible to everyone.
"""
import logging

from sqlalchemy import delete, select

from app.db import Database, Document, DocumentAccess, Group, KBAccess, User
from app.knowledge import kbs, metadata

log = logging.getLogger(__name__)
VISIBILITY = ("everyone", "people", "groups")


def ids_for(db: Database, doc_id: int) -> tuple[list[int], list[int]]:
    """(group ids, person ids) the document is shared with; both empty for Everyone."""
    with db.session() as s:
        doc = s.get(Document, doc_id)
        if doc is None or doc.visibility == "everyone":
            return [], []
        rows = list(s.scalars(select(DocumentAccess).where(DocumentAccess.document_id == doc_id)))
        groups = sorted(r.ref_id for r in rows if r.kind == "group")
        people = sorted(r.ref_id for r in rows if r.kind == "user")
        return (groups, []) if doc.visibility == "groups" else ([], people)


def get(db: Database, doc_id: int) -> dict:
    with db.session() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            raise ValueError("No such document.")
        visibility = doc.visibility
    groups, people = ids_for(db, doc_id)
    return {"visibility": visibility, "group_ids": groups, "user_ids": people}


def clear(db: Database, doc_id: int, session=None) -> None:
    if session is not None:
        session.execute(delete(DocumentAccess).where(DocumentAccess.document_id == doc_id))
        return
    with db.session() as s:
        s.execute(delete(DocumentAccess).where(DocumentAccess.document_id == doc_id))


def sync_chunks(db: Database, doc: dict) -> int:
    """Write the document's current access into the metadata of its chunks (no re-embedding)."""
    kb = kbs.get_kb(db, doc["kb_id"])
    if kb is None:
        return 0
    groups, people = ids_for(db, doc["id"])
    patch = {"allowed_groups": metadata.encode_ids(groups), "allowed_people": metadata.encode_ids(people)}
    return kbs.store_for(kb).update_metadata(patch, document=doc["doc_key"])


def set_access(db: Database, doc_id: int, visibility: str, user_ids=(), group_ids=()) -> dict:
    """Change who can see a document. Validates, stores, and updates the chunks in place."""
    from app.knowledge import documents
    if visibility not in VISIBILITY:
        raise ValueError("Visible to must be Everyone, Selected people or Selected groups.")
    users, groups = sorted({int(i) for i in user_ids or ()}), sorted({int(i) for i in group_ids or ()})
    with db.session() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            raise ValueError("No such document.")
        if visibility == "people":
            if not users:
                raise ValueError("Choose at least one person, or make the document visible to Everyone.")
            if set(s.scalars(select(User.id).where(User.id.in_(users)))) != set(users):
                raise ValueError("One of the chosen people does not exist.")
        if visibility == "groups":
            if not groups:
                raise ValueError("Choose at least one group, or make the document visible to Everyone.")
            if set(s.scalars(select(Group.id).where(Group.id.in_(groups)))) != set(groups):
                raise ValueError("One of the chosen groups does not exist.")
        s.execute(delete(DocumentAccess).where(DocumentAccess.document_id == doc_id))
        if visibility == "people":
            s.add_all(DocumentAccess(document_id=doc_id, kind="user", ref_id=i) for i in users)
        elif visibility == "groups":
            s.add_all(DocumentAccess(document_id=doc_id, kind="group", ref_id=i) for i in groups)
        doc.visibility = visibility
    view = documents.get(db, doc_id)
    sync_chunks(db, view)
    return get(db, doc_id)



def describe(db: Database, doc_id: int) -> str:
    """A short text for the audit log."""
    a = get(db, doc_id)
    if a["visibility"] == "everyone":
        return "visible to Everyone"
    return f"visible to {len(a['group_ids']) if a['visibility'] == 'groups' else len(a['user_ids'])} " \
           f"{'group(s)' if a['visibility'] == 'groups' else 'person(s)'}"


# ---- enforcement: who may ask which knowledge base, and which documents they may see -------------------
def manages(who, kb: dict) -> bool:
    """Admins manage everything; an Editor manages the knowledge bases of the departments they edit."""
    return who.role == "admin" or (who.role == "editor" and kb["department_id"] in who.edits_departments)


def kb_query_groups(db: Database, kb_id: int) -> list[int]:
    with db.session() as s:
        return sorted(s.scalars(select(KBAccess.group_id).where(KBAccess.kb_id == kb_id)))


def set_kb_query_groups(db: Database, kb_id: int, group_ids) -> list[int]:
    """Empty = everyone on a channel bound to the knowledge base may ask it; otherwise only these groups."""
    ids = sorted({int(i) for i in group_ids or ()})
    with db.session() as s:
        if ids and set(s.scalars(select(Group.id).where(Group.id.in_(ids)))) != set(ids):
            raise ValueError("One of the chosen groups does not exist.")
        s.execute(delete(KBAccess).where(KBAccess.kb_id == kb_id))
        s.add_all(KBAccess(kb_id=kb_id, group_id=i) for i in ids)
    return ids


def can_query(db: Database, kb: dict, who) -> bool:
    """Knowledge base permission (D17). Both this AND document visibility are needed to see a passage."""
    if not who.active:
        return False
    if manages(who, kb):
        return True
    allowed = kb_query_groups(db, kb["id"])
    return not allowed or bool(set(allowed) & set(who.group_ids))


def visible_doc_keys(db: Database, kb: dict, who) -> list[str]:
    """The `doc_id` values of the documents in this knowledge base that `who` may see, worked out from the database NOW
    (group membership and status are not remembered anywhere). Chunks that belong to no known document are never returned."""
    return list(visible_docs(db, kb, who))


def visible_docs(db: Database, kb: dict, who) -> dict[str, int]:
    """Same as `visible_doc_keys`, as {doc_id stored on the chunks: Document id}."""
    if not who.active:
        return {}
    with db.session() as s:
        docs = list(s.execute(select(Document.id, Document.doc_key, Document.visibility).where(Document.kb_id == kb["id"])).all())
        rows = list(s.scalars(select(DocumentAccess).where(DocumentAccess.document_id.in_([d[0] for d in docs])))) if docs else []
    if manages(who, kb):
        return {key: doc_id for doc_id, key, _ in docs}
    shared: dict[int, set] = {}
    for r in rows:
        shared.setdefault(r.document_id, set()).add((r.kind, r.ref_id))
    mine = {("group", g) for g in who.group_ids} | ({("user", who.user_id)} if who.user_id is not None else set())
    out = {}
    for doc_id, key, visibility in docs:
        if visibility == "everyone":
            out[key] = doc_id
        elif visibility in ("groups", "people"):
            wanted = {x for x in shared.get(doc_id, set()) if x[0] == ("group" if visibility == "groups" else "user")}
            if wanted & mine:
                out[key] = doc_id
        # any other value: hidden (fail closed)
    return out


def permits(meta: dict, who, kb: dict) -> bool:
    """Second line of defence on every returned passage, using the access stored in the chunk itself."""
    if manages(who, kb):
        return True
    groups, people = metadata.decode_ids(meta.get("allowed_groups", "")), metadata.decode_ids(meta.get("allowed_people", ""))
    if not groups and not people:
        return True
    return bool(set(groups) & set(who.group_ids)) or (who.user_id is not None and who.user_id in people)
