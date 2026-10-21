"""The document list says who each restricted document is visible to, by name, and only for the kind it is set to."""
from types import SimpleNamespace

from app.admin.documents_api import _access_names
from app.db import Document, DocumentAccess, Group, User


def test_names_for_groups_people_and_everyone(new_db):
    db = new_db()
    with db.session() as s:
        s.add_all([Group(id=1, name="HR leads"), Group(id=2, name="Auditors"),
                   User(id=10, email="ana@sysfore.com", name="Ana"), User(id=11, email="bo@sysfore.com", name="")])
        s.add_all([Document(id=1, kb_id=1, name="a.pdf", doc_key="a", visibility="groups"),
                   Document(id=2, kb_id=1, name="b.pdf", doc_key="b", visibility="people"),
                   Document(id=3, kb_id=1, name="c.pdf", doc_key="c", visibility="everyone")])
        s.add_all([DocumentAccess(document_id=1, kind="group", ref_id=2), DocumentAccess(document_id=1, kind="group", ref_id=1),
                   DocumentAccess(document_id=1, kind="user", ref_id=10),          # stale row of the other kind: ignored
                   DocumentAccess(document_id=2, kind="user", ref_id=11), DocumentAccess(document_id=2, kind="user", ref_id=10)])
    docs = [{"id": 1, "visibility": "groups"}, {"id": 2, "visibility": "people"}, {"id": 3, "visibility": "everyone"}]
    names = _access_names(SimpleNamespace(db=db), docs)
    assert names[1] == ["Auditors", "HR leads"]                    # sorted, groups only
    assert names[2] == ["Ana", "bo@sysfore.com"]                  # email when there is no name
    assert 3 not in names


def test_no_restricted_documents_means_no_query(new_db):
    assert _access_names(SimpleNamespace(db=None), [{"id": 1, "visibility": "everyone"}]) == {}
