"""Release 3, Phase 2: the ticket admin API: who sees what, the moves, the manual "Create ticket", the queue release and the report (A3, A8, D63)."""
import types

import pytest
from sqlalchemy import delete, select

from app import global_settings, scoped_settings, ticket_agent, ticketing, tickets
from app.admin import users
from app.db import Alert, Department, DepartmentEditor, Document, Feedback, KnowledgeBase, ScopedSetting, Ticket, TicketEvent, get_db
from tests.test_release2_suggestions import P, PW, person, sign_in
from tests.test_ticketing import make_feedback

T = P + "/tickets"


@pytest.fixture(scope="module")
def world():
    db = get_db()
    w = types.SimpleNamespace(db=db)
    with db.session() as s:
        for name in ("TkApiHR", "TkApiIT"):
            s.add(Department(name=name, retention_days=30, banned_topics="", is_default=False))
    with db.session() as s:
        w.dept, w.other_dept = [s.scalar(select(Department.id).where(Department.name == n)) for n in ("TkApiHR", "TkApiIT")]
        s.add(KnowledgeBase(name="TkApi A", department_id=w.dept, collection="tkapi_a"))
        s.add(KnowledgeBase(name="TkApi B", department_id=w.other_dept, collection="tkapi_b"))
    with db.session() as s:
        w.kb_a = s.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == "TkApi A"))
        w.kb_b = s.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == "TkApi B"))
        w.docs = {}
        for key in ("leave", "expense", "it", "board"):
            d = Document(kb_id=w.kb_a, name=f"{key}.txt", doc_key=key, status="Indexed")
            s.add(d)
            s.flush()
            w.docs[key] = d.id
    users.create_admin(db, "tkapi-boss@sysfore.com", PW, domain="sysfore.com")
    w.ed = person(db, "tkapi-ed@sysfore.com", "editor", w.dept)
    w.ed2 = person(db, "tkapi-ed2@sysfore.com", "editor", w.other_dept)
    with db.session() as s:
        s.add(DepartmentEditor(department_id=w.dept, user_id=w.ed["id"]))
        s.add(DepartmentEditor(department_id=w.other_dept, user_id=w.ed2["id"]))
    w.admin, w.ah = sign_in("tkapi-boss@sysfore.com")
    w.editor, w.eh = sign_in("tkapi-ed@sysfore.com")
    w.editor2, w.eh2 = sign_in("tkapi-ed2@sysfore.com")
    yield w
    with db.session() as s:
        s.execute(delete(Document).where(Document.kb_id.in_([w.kb_a, w.kb_b])))
        s.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_([w.kb_a, w.kb_b])))


@pytest.fixture(autouse=True)
def clean(world, monkeypatch):
    monkeypatch.setattr(ticket_agent, "title", lambda item: None)
    monkeypatch.setattr(ticket_agent, "same_issue", lambda item, cands: set())
    yield
    with get_db().session() as s:
        s.execute(delete(TicketEvent))
        s.execute(delete(Ticket))
        s.execute(delete(Feedback))
        s.execute(delete(ScopedSetting))
        s.execute(delete(Alert).where(Alert.key.like("ticket:%")))
    scoped_settings.invalidate()
    global_settings.update({"admin_agent_mode": "off", "ticket_assignment": "department", "ticket_related_window_days": 0}, "t", get_db())


def raise_ticket(world, **kw):
    return ticketing.new(world.db, make_feedback(world, **kw))["ticket_id"]


def audit_actions():
    from app.channels import get_store
    return [a["action"] for a in get_store().recent_audit(50)]


def test_an_admin_works_a_ticket_from_the_department_queue_to_completed(world):
    tid = raise_ticket(world)
    listed = world.admin.get(T, headers=world.ah).json()
    assert listed["total"] == 1 and listed["items"][0]["status"] == "assigned_to_human" and listed["items"][0]["kb"] == "TkApi A"
    detail = world.admin.get(f"{T}/{tid}", headers=world.ah).json()
    assert detail["type"] == "feedback" and detail["evidence"] and [e["event"] for e in detail["events"]] == ["new", "assigned_to_human"]
    assert world.admin.post(f"{T}/{tid}/take", headers=world.ah, json={}).json()["status"] == "in_progress"
    assert world.admin.post(f"{T}/{tid}/hold", headers=world.ah, json={"note": "waiting for the owner"}).json()["status"] == "hold"
    assert world.admin.post(f"{T}/{tid}/take", headers=world.ah, json={}).json()["status"] == "in_progress"
    assert world.admin.post(f"{T}/{tid}/comment", headers=world.ah, json={"note": "Checked the policy page"}).status_code == 200
    done = world.admin.post(f"{T}/{tid}/complete", headers=world.ah, json={"note": "Re-ingested the file"}).json()
    assert done["status"] == "completed" and done["closed_at"]
    assert tickets.status_matches_events(world.db, tid)
    assert {"ticket.take", "ticket.hold", "ticket.complete"} <= set(audit_actions())


def test_a_note_is_needed_to_hold_complete_or_dismiss_and_a_closed_ticket_stays_closed(world):
    tid = raise_ticket(world)
    world.admin.post(f"{T}/{tid}/take", headers=world.ah, json={})
    for action in ("hold", "complete", "dismiss"):
        assert world.admin.post(f"{T}/{tid}/{action}", headers=world.ah, json={"note": " "}).status_code == 422
    assert world.admin.post(f"{T}/{tid}/dismiss", headers=world.ah, json={"note": "Not a real problem"}).json()["status"] == "dismissed"
    assert world.admin.post(f"{T}/{tid}/take", headers=world.ah, json={}).status_code == 409           # a closed ticket is not reopened
    assert world.admin.post(f"{T}/{tid}/complete", headers=world.ah, json={"note": "again please"}).status_code == 409


def test_an_editor_sees_only_the_tickets_of_the_departments_they_edit(world):
    mine = raise_ticket(world, user_id=201)
    ticketing.new(world.db, make_feedback(world, user_id=202, kb_id=world.kb_b))                         # another department's KB
    assert world.editor.get(T, headers=world.eh).json()["total"] == 1
    assert world.editor.get(f"{T}/{mine}", headers=world.eh).status_code == 200
    assert world.admin.get(T, headers=world.ah).json()["total"] == 2
    other = world.editor2.get(T, headers=world.eh2).json()["items"][0]["id"]
    assert world.editor.get(f"{T}/{other}", headers=world.eh).status_code == 404                        # not theirs: it does not exist for them
    assert world.editor.post(f"{T}/{other}/take", headers=world.eh, json={}).status_code == 404
    assert world.editor.get(T, headers=world.eh, params={"kb_id": world.kb_b}).json()["total"] == 0
    assert world.editor.get(T, headers=world.eh, params={"status": "nonsense"}).status_code == 422


def test_reassigning_a_ticket_in_the_agents_queue_moves_it_to_a_person_and_alerts_them(world):
    scoped_settings.put("kb", world.kb_a, {"ticket_assignment": "autonomous"}, "t", world.db)
    global_settings.update({"admin_agent_mode": "live"}, "t", world.db)
    tid = raise_ticket(world)
    assert world.admin.get(f"{T}/{tid}", headers=world.ah).json()["status"] == "assigned"
    r = world.admin.post(f"{T}/{tid}/reassign", headers=world.ah, json={"to_type": "user", "to_id": world.ed["id"], "note": "Please look"})
    assert r.status_code == 200 and r.json()["status"] == "assigned_to_human" and r.json()["assignee_id"] == world.ed["id"]
    assert world.admin.post(f"{T}/{tid}/reassign", headers=world.ah, json={"to_type": "team", "to_id": 1}).status_code == 400
    assert world.editor.post(f"{T}/{tid}/take", headers=world.eh, json={}).json()["assignee_type"] == "user"


def test_switching_the_agent_off_releases_its_queue_and_audits_it(world):
    scoped_settings.put("kb", world.kb_a, {"ticket_assignment": "autonomous"}, "t", world.db)
    assert world.admin.put(P + "/settings", headers=world.ah, json={"values": {"admin_agent_mode": "live"}}).status_code == 200
    ids = [raise_ticket(world, user_id=300 + i) for i in range(2)]
    assert {world.admin.get(f"{T}/{i}", headers=world.ah).json()["status"] for i in ids} == {"assigned"}
    assert world.admin.put(P + "/settings", headers=world.ah, json={"values": {"admin_agent_mode": "off"}}).status_code == 200
    assert {world.admin.get(f"{T}/{i}", headers=world.ah).json()["status"] for i in ids} == {"assigned_to_human"}
    assert "ticket.release_queue" in audit_actions()


def test_a_manual_ticket_comes_from_a_negative_feedback_in_the_users_scope_and_only_once(world):
    fid = make_feedback(world)
    first = world.admin.post(T + "/new", headers=world.ah, json={"feedback_id": fid})
    assert first.status_code == 200 and first.json()["outcome"] == "new"
    again = world.admin.post(T + "/new", headers=world.ah, json={"feedback_id": fid}).json()
    assert again["ticket_id"] == first.json()["ticket_id"] and again["existing"] is True
    assert "ticket.create" in audit_actions()
    up = make_feedback(world, rating="up", comment="")
    assert world.admin.post(T + "/new", headers=world.ah, json={"feedback_id": up}).status_code == 422
    elsewhere = make_feedback(world, kb_id=world.kb_b, user_id=401)
    assert world.editor.post(T + "/new", headers=world.eh, json={"feedback_id": elsewhere}).status_code == 404
    assert world.editor2.post(T + "/new", headers=world.eh2, json={"feedback_id": elsewhere}).status_code == 200
    assert world.admin.post(T + "/new", headers=world.ah, json={"feedback_id": 99999}).status_code == 404


def test_the_report_counts_by_status_age_and_evidence_without_any_text(world):
    a = raise_ticket(world, user_id=501)
    ticketing.new(world.db, make_feedback(world, user_id=501))                                           # evidence added to the open ticket
    b = raise_ticket(world, user_id=502)
    world.admin.post(f"{T}/{b}/take", headers=world.ah, json={})
    world.admin.post(f"{T}/{b}/complete", headers=world.ah, json={"note": "Fixed it"})
    rep = world.admin.get(T + "/report", headers=world.ah).json()
    assert rep["total"] == 2 and rep["evidence_additions"] == 1 and rep["closed"] == 1 and rep["average_hours_to_close"] is not None
    assert {d["name"]: d["count"] for d in rep["by_status"]} == {"assigned_to_human": 1, "completed": 1}
    assert rep["open_by_age"]["under_1_day"] == 1 and "The figure is wrong" not in str(rep)
    assert world.editor2.get(T + "/report", headers=world.eh2).json()["total"] == 0


def test_only_admins_and_editors_have_the_tickets_screen_and_nobody_else_gets_in(world):
    from fastapi.testclient import TestClient
    from app.admin import roles
    from app.api import app
    assert "tickets" in roles.screens_for("admin") and "tickets" in roles.screens_for("editor") and roles.screens_for("viewer") == []
    assert roles.allowed("editor", T) and not roles.allowed("viewer", T)
    anon = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), raise_server_exceptions=False)
    assert anon.get(T).status_code == 401 and anon.post(T + "/new", json={"feedback_id": 1}).status_code == 401


def test_retention_deletes_only_old_closed_tickets_with_their_events_and_keeps_the_rest(world):
    from datetime import datetime, timedelta, timezone
    old, recent, openn = raise_ticket(world, user_id=601), raise_ticket(world, user_id=602), raise_ticket(world, user_id=603)
    for t in (old, recent):
        world.admin.post(f"{T}/{t}/take", headers=world.ah, json={})
        world.admin.post(f"{T}/{t}/complete", headers=world.ah, json={"note": "done"})
    later = ticketing.new(world.db, make_feedback(world, user_id=601))["ticket_id"]                      # a new ticket linked to the closed one
    with world.db.session() as s:
        s.get(Ticket, old).closed_at = datetime.now(timezone.utc) - timedelta(days=40)
        s.get(Ticket, recent).closed_at = datetime.now(timezone.utc) - timedelta(days=2)
    assert ticketing.retire(world.db) == 0                                                              # 0 days = keep for ever
    global_settings.update({"ticket_retention_days": 30}, "t", world.db)
    from app import retention
    assert retention.run(world.db)["tickets"] == 1
    with world.db.session() as s:
        assert s.get(Ticket, old) is None and s.get(Ticket, recent) and s.get(Ticket, openn)
        assert s.scalar(select(TicketEvent.id).where(TicketEvent.ticket_id == old)) is None
        assert s.get(Ticket, later).related_ticket_id is None
    global_settings.update({"ticket_retention_days": 0}, "t", world.db)
