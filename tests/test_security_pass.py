"""Phase 11 security pass: privilege escalation on every admin route, secrets through every API, cross-department access,
permission bypass through the router, summariser and preview paths, request forgery, upload handling and one-time links.
Runs against the real application object with real data (documents are uploaded, embedded and searched)."""
import json
import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import chat_service, rag
from app.admin import providers_api, roles, users
from app.api import app
from app.db import Department, DepartmentEditor, Message, get_db
from app.knowledge import documents, embeddings, kbs
from tests.test_exposure import routes

P = "/admin/api"
PW = "a good password 1"
SECRETS = {"provider": "sk-PROVIDER-SECRET-9f3a", "slack_bot": "xoxb-SLACK-BOT-SECRET-77", "slack_app": "xapp-SLACK-APP-SECRET-88",
           "cohere": "COHERE-KB-SECRET-5521", "pg": "postgresql://u:PGPASSWORD-SECRET-31@db.internal:5432/x",
           "hook": "https://hooks.example.com/services/T0/B0/WEBHOOKSECRET41", "smtp": "SMTP-PASSWORD-SECRET-62"}
RESTRICTED = "ZEBRA-RESTRICTED-4471"


def client(host="localhost"):
    return TestClient(app, base_url=f"http://{host}", client=("127.0.0.1", 50000), raise_server_exceptions=False)


def sign_in(email, pw=PW):
    c = client()
    r = c.post(P + "/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, (email, r.status_code, r.text)
    return c, {"X-CSRF-Token": r.json()["csrf"]}


def person(db, email, role, department_id=None):
    u = users.create_user(db, email, email.split("@")[0], role, department_id)
    t, _ = users.issue_set_password_link(db, u["id"], "setup")
    users.redeem_set_password_link(db, t, PW)
    return u


def fill(path):
    """Concrete URL for a route pattern: ids become 1, the channel type slack."""
    return re.sub(r"\{(\w+)\}", lambda m: "slack" if m.group(1) == "typ" else "a@b.c" if m.group(1) == "email" else "1", path)


@pytest.fixture(scope="module")
def world():
    mp = pytest.MonkeyPatch()
    mp.setattr(providers_api, "_check", lambda type_, cfg: ["model-a"])
    db = get_db()
    kbs.ensure_default(db)
    w = type("W", (), {})()
    w.db = db
    with db.session() as s:
        s.add(Department(name="SecHR", retention_days=90, banned_topics="", is_default=False))
        s.add(Department(name="SecIT", retention_days=90, banned_topics="", is_default=False))
    with db.session() as s:
        w.hr = s.scalar(select(Department.id).where(Department.name == "SecHR"))
        w.it = s.scalar(select(Department.id).where(Department.name == "SecIT"))
    users.create_admin(db, "sec-boss@sysfore.com", PW, domain="sysfore.com")
    w.ed_hr = person(db, "sec-ed-hr@sysfore.com", "editor", w.hr)
    w.ed_it = person(db, "sec-ed-it@sysfore.com", "editor", w.it)
    with db.session() as s:
        s.add(DepartmentEditor(department_id=w.hr, user_id=w.ed_hr["id"]))
        s.add(DepartmentEditor(department_id=w.it, user_id=w.ed_it["id"]))
    w.viewer = person(db, "sec-viewer@sysfore.com", "viewer", w.hr)
    w.admin, w.ah = sign_in("sec-boss@sysfore.com")
    w.edhr, w.eh = sign_in("sec-ed-hr@sysfore.com")
    w.edit, w.ei = sign_in("sec-ed-it@sysfore.com")

    # plant a secret in every place the system keeps one
    w.admin.put(P + "/providers/approval", headers=w.ah, json={"approved": ["openai"]})
    pr = w.admin.post(P + "/providers", headers=w.ah, json={"type": "openai", "name": "SecMain", "values": {"api_key": SECRETS["provider"]}})
    assert pr.status_code == 201, pr.text
    w.admin.put(P + "/model-settings", headers=w.ah, json={"preferred": {"provider_id": pr.json()["id"], "model": "model-a"}, "request_timeout": 30})
    from app.channels import get_store
    get_store().save("slack", enabled=False, values={"bot_token": SECRETS["slack_bot"], "app_token": SECRETS["slack_app"]}, actor="sec-boss@sysfore.com")
    w.kb_hr = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "SecHR docs", "department_id": w.hr}).json()
    w.kb_it = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "SecIT docs", "department_id": w.it}).json()
    embeddings.save_kb_secrets(db, w.kb_hr["id"], {"api_key": SECRETS["cohere"]})
    embeddings.save_kb_secrets(db, w.kb_hr["id"], {"dsn": SECRETS["pg"]}, owner=kbs.STORE_SECRET_OWNER)
    r = w.admin.put(P + "/alert-sinks", headers=w.ah, json={"values": {"webhook_enabled": True}, "secrets": {"webhook_url": SECRETS["hook"], "smtp_password": SECRETS["smtp"]}})
    assert r.status_code == 200, r.text

    def upload(c, h, kb, name, text):
        r = c.post(f"{P}/kbs/{kb['id']}/documents", headers=h, files={"file": (name, text.encode())})
        assert r.status_code == 201, r.text
        end = time.time() + 90
        while time.time() < end and documents.get(db, r.json()["id"])["status"] not in ("Indexed", "Failed"):
            time.sleep(0.2)
        return r.json()
    w.doc_hr = upload(w.edhr, w.eh, w.kb_hr, "hr-secret.txt", f"The HR vault code is {RESTRICTED} and is for the HR team only.")
    w.doc_it = upload(w.edit, w.ei, w.kb_it, "it-notes.txt", "The IT maintenance window is Sunday night.")
    yield w
    documents.shutdown()
    mp.undo()


# ---- 1. privilege escalation -----------------------------------------------------------------------------------------
def admin_routes():
    return sorted({(m, p) for m, p in routes() if p.startswith(P)})


def test_the_enumeration_finds_the_admin_api():
    assert len(admin_routes()) > 80


def test_an_editor_is_refused_on_every_route_their_role_does_not_list(world):
    """Default deny: any /admin/api path outside the Editor's prefixes answers 403, whatever the method."""
    bad = []
    for method, path in admin_routes():
        if roles.allowed("editor", path):
            continue
        r = world.edhr.request(method, fill(path), headers=world.eh, json={} if method in ("POST", "PUT") else None)
        if r.status_code != 403:
            bad.append((method, path, r.status_code))
    assert bad == [], bad


def test_the_editor_and_viewer_route_lists_are_exactly_what_was_approved():
    """Pinned on purpose: widening an Editor's reach must be a visible change to this test, not a quiet edit to the map."""
    assert roles.PREFIXES["editor"] == tuple(P + x for x in ("/auth", "/me", "/departments", "/kbs", "/documents", "/groups", "/directory", "/prompts", "/usage"))
    assert roles.PREFIXES["viewer"] == ()
    assert roles.PREFIXES["admin"] == (P,)
    assert not roles.allowed("editor", P + "/providers") and not roles.allowed("editor", P + "/users") and not roles.allowed("editor", P + "/reports/guardrails")
    assert not roles.allowed("viewer", P + "/auth/me") and not roles.allowed("nobody", P + "/me/permissions")


def test_anonymous_is_refused_on_every_admin_route(world):
    anon = client()
    # Public on purpose: password sign-in, and Microsoft/Google sign-in (which buttons to show, start, and the return
    # address). Those three only list provider names or redirect; tests/test_sso.py covers what they accept.
    public = ("/auth/login", "/auth/set-password", "/auth/logout", "/auth/sso", "/auth/sso/{provider}/start", "/auth/sso/{provider}/callback")
    bad = [(m, p, anon.request(m, fill(p), json={} if m in ("POST", "PUT") else None).status_code) for m, p in admin_routes()
           if not p.endswith(public)]
    assert [b for b in bad if b[2] not in (401, 403)] == [], [b for b in bad if b[2] not in (401, 403)]
    assert anon.get("/admin/api/auth/sso").json() == {"providers": []}               # nothing else, signed in or not


def test_editors_cannot_use_the_admin_only_parts_of_the_routes_they_do_have(world):
    ed, h = world.edhr, world.eh
    assert ed.put(P + "/prices", headers=h, json={"provider_type": "openai", "model": "m", "input_per_1k": 1, "output_per_1k": 1}).status_code == 403
    assert ed.delete(P + "/prices/1", headers=h).status_code == 403
    assert ed.post(P + "/departments", headers=h, json={"name": "Evil", "retention_days": 1}).status_code == 403
    assert ed.delete(f"{P}/departments/{world.it}", headers=h).status_code in (403, 404)
    assert ed.put(f"{P}/departments/{world.it}", headers=h, json={"retention_days": 1}).status_code in (403, 404)
    assert ed.post(P + "/prompts/drafts", headers=h, json={"scope": "base-answer", "text": "x"}).status_code == 403
    assert ed.get(P + "/prompts/versions", params={"scope": "base-answer"}, headers=h).status_code == 403
    assert ed.post(P + "/prompts/preview", headers=h, json={"scope": "base-chat", "text": "x", "question": "hi"}).status_code == 403
    assert ed.get(P + "/me/alerts").json() is not None
    r = ed.put(f"{P}/departments/{world.hr}", headers=h, json={"retention_days": 99999})
    assert r.status_code in (403, 422)


def test_an_editor_cannot_promote_themselves_or_change_roles(world):
    r = world.edhr.put(f"{P}/users/{world.ed_hr['id']}", headers=world.eh, json={"role": "admin"})
    assert r.status_code == 403
    assert users.get_user(world.db, "sec-ed-hr@sysfore.com")["role"] == "editor"


def test_a_viewer_cannot_sign_in_to_the_admin_page(world):
    r = client().post(P + "/auth/login", json={"email": "sec-viewer@sysfore.com", "password": PW})
    assert r.status_code in (401, 403)


def test_a_deactivated_editor_loses_access_at_once(world):
    u = person(world.db, "sec-temp@sysfore.com", "editor", world.hr)
    c, h = sign_in("sec-temp@sysfore.com")
    assert c.get(P + "/auth/me").status_code == 200
    users.update_user(world.db, u["id"], status="Deactivated")
    assert c.get(P + "/auth/me").status_code == 401
    assert c.get(P + "/kbs").status_code == 401


# ---- 2. secrets never come back ---------------------------------------------------------------------------------------
def test_no_get_route_ever_returns_a_stored_secret(world):
    leaks = []
    for c, who in ((world.admin, "admin"), (world.edhr, "editor")):
        for method, path in admin_routes():
            if method != "GET":
                continue
            r = c.get(fill(path))
            for name, value in SECRETS.items():
                if value.split("://")[-1][:20] in r.text or value in r.text:
                    leaks.append((who, path, name))
    assert leaks == [], leaks


def test_no_secret_is_in_write_responses_the_audit_log_or_the_database(world):
    admin = world.admin
    r = admin.put(P + "/alert-sinks", headers=world.ah, json={"values": {"webhook_enabled": True}, "secrets": {"webhook_url": SECRETS["hook"]}})
    assert SECRETS["hook"] not in r.text
    audit = json.dumps(admin.get(P + "/audit/search", params={"limit": 200}).json())
    for value in SECRETS.values():
        assert value not in audit
    from app.db import Secret
    with world.db.session() as s:
        stored = " ".join(x.ciphertext for x in s.scalars(select(Secret)))
    assert stored and not any(v in stored for v in SECRETS.values())


# ---- 3. cross-department access ---------------------------------------------------------------------------------------
def test_an_editor_cannot_see_or_touch_another_departments_knowledge(world):
    ed, h = world.edhr, world.eh
    assert [k["name"] for k in ed.get(P + "/kbs").json()["kbs"] if k["name"].startswith("Sec")] == ["SecHR docs"]
    assert ed.get(f"{P}/kbs/{world.kb_it['id']}/documents").status_code == 404
    assert ed.post(f"{P}/kbs/{world.kb_it['id']}/documents", headers=h, files={"file": ("x.txt", b"x")}).status_code in (403, 404)
    assert ed.get(f"{P}/documents/{world.doc_it['id']}/access").status_code == 404
    assert ed.put(f"{P}/documents/{world.doc_it['id']}/access", headers=h, json={"visibility": "everyone"}).status_code == 404
    assert ed.delete(f"{P}/documents/{world.doc_it['id']}", headers=h).status_code == 404
    assert ed.post(f"{P}/documents/{world.doc_it['id']}/reindex", headers=h).status_code == 404
    assert ed.put(f"{P}/kbs/{world.kb_it['id']}", headers=h, json={"name": "mine now"}).status_code in (403, 404)
    assert ed.delete(f"{P}/kbs/{world.kb_it['id']}", headers=h).status_code in (403, 404)
    assert ed.get(f"{P}/kbs/{world.kb_it['id']}/index").status_code in (403, 404)
    assert documents.get(world.db, world.doc_it["id"]) is not None


def test_an_editor_cannot_move_a_document_into_or_out_of_another_department(world):
    ed, h = world.edhr, world.eh
    assert ed.post(f"{P}/documents/{world.doc_hr['id']}/move", headers=h, json={"kb_id": world.kb_it["id"]}).status_code in (403, 404)
    assert ed.post(f"{P}/documents/{world.doc_it['id']}/move", headers=h, json={"kb_id": world.kb_hr["id"]}).status_code in (403, 404)
    assert documents.get(world.db, world.doc_hr["id"])["kb_id"] == world.kb_hr["id"]


def test_an_editor_cannot_read_another_departments_prompts_groups_or_usage(world):
    ed, h = world.edhr, world.eh
    assert ed.get(P + "/prompts/versions", params={"scope": "dept", "department_id": world.it}, headers=h).status_code in (403, 404)
    assert ed.post(P + "/prompts/drafts", headers=h, json={"scope": "dept", "department_id": world.it, "text": "x"}).status_code in (403, 404)
    assert ed.post(P + "/groups", headers=h, json={"name": "g", "department_id": world.it}).status_code in (403, 404)
    assert ed.get(P + "/usage", params={"department": "SecIT"}).json()["totals"]["calls"] == 0


# ---- 4. permission bypass through the router, summariser and preview -------------------------------------------------
@pytest.fixture
def spy(monkeypatch, world):
    """Every message sent to any model, for any purpose."""
    seen = []

    class Spy:
        def invoke(self, messages, **kw):
            seen.append(("invoke", kw.get("purpose", ""), "\n".join(str(m.content) for m in messages)))
            q = str(messages[-1].content).split("Latest message:", 1)[-1].strip()
            return type("M", (), {"content": json.dumps({"search": True, "query": q}), "usage_metadata": None})()

        def stream(self, messages, **kw):
            seen.append(("stream", kw.get("purpose", ""), "\n".join(str(m.content) for m in messages)))
            yield type("M", (), {"content": "Answer based on the documents [1].", "usage_metadata": None})()
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: Spy())
    monkeypatch.setattr(rag, "get_chat_model", lambda: Spy())
    import app.ratelimit as rl
    monkeypatch.setattr(rl, "allow", lambda key, limit, now=None: (True, 0))
    return seen


def ask(email, text, kb_ids):
    from app.channels import core
    from app.channels.base import Envelope
    return core.handle(Envelope("slack", "SEC", "room", f"th-{time.time_ns()}", "U", text, tuple(kb_ids), email))


def test_restricted_text_reaches_no_model_call_of_any_kind_for_an_outsider(world, spy):
    from app.knowledge import access
    access.set_access(world.db, world.doc_hr["id"], "people", user_ids=[world.ed_hr["id"]])      # visible to the HR Editor only
    Q = "What is the HR vault code?"
    res = ask("sec-viewer@sysfore.com", Q, [world.kb_hr["id"]])                                   # an HR Viewer, but not on the list
    assert not any(RESTRICTED in text for _, _, text in spy)
    assert not any(s["source"] == "hr-secret.txt" for s in res["sources"])
    spy.clear()
    res = ask("sec-ed-hr@sysfore.com", Q, [world.kb_hr["id"]])
    assert any(RESTRICTED in text for _, _, text in spy), "sanity check: a permitted person does get the passage"


def test_follow_up_turns_and_summaries_do_not_carry_restricted_text_to_an_outsider(world, spy):
    from app import summaries
    for i in range(3):
        ask("sec-viewer@sysfore.com", f"What is the HR vault code? ({i})", [world.kb_hr["id"]])
    assert not any(RESTRICTED in text for _, _, text in spy)
    with world.db.session() as s:
        stored = " ".join(m.content for m in s.scalars(select(Message).where(Message.conversation_id.like("slack-SEC-%"))))
    assert RESTRICTED not in stored


def test_preview_cannot_search_a_knowledge_base_the_editor_does_not_manage(world, spy):
    r = world.edit.post(P + "/prompts/preview", headers=world.ei, json={"scope": "dept", "department_id": world.it, "text": "Be brief.",
                                                                          "question": "What is the HR vault code?", "kb_ids": [world.kb_hr["id"]]})
    assert r.status_code in (403, 404, 422) or RESTRICTED not in r.text
    assert not any(RESTRICTED in text for _, _, text in spy)


def test_chunk_preview_of_another_department_is_refused(world):
    r = world.edhr.post(f"{P}/kbs/{world.kb_it['id']}/chunk-preview", headers=world.eh, data={"strategy": "recursive"}, files={"file": ("a.txt", b"hello world " * 50)})
    assert r.status_code in (403, 404)


# ---- 5. request forgery, uploads and one-time links ---------------------------------------------------------------------
def test_every_state_changing_admin_route_needs_the_csrf_token(world):
    missing = []
    for method, path in admin_routes():
        if method == "GET" or path.endswith(("/auth/login", "/auth/set-password", "/auth/logout")):
            continue
        r = world.admin.request(method, fill(path), json={})        # signed in, but no X-CSRF-Token header
        if r.status_code not in (401, 403):
            missing.append((method, path, r.status_code))
    assert missing == [], missing


def test_a_wrong_csrf_token_is_refused(world):
    assert world.admin.post(P + "/departments", headers={"X-CSRF-Token": "forged"}, json={"name": "Nope", "retention_days": 5}).status_code == 403


def test_upload_names_cannot_escape_the_documents_folder(world):
    r = world.edhr.post(f"{P}/kbs/{world.kb_hr['id']}/documents", headers=world.eh, files={"file": ("../../evil.txt", b"just text")})
    if r.status_code == 201:
        d = documents.get(world.db, r.json()["id"])
        assert ".." not in d["name"] and "/" not in d["name"] and "\\" not in d["name"]
        assert documents.doc_dir(world.kb_hr["id"], d["id"]).resolve().is_relative_to(documents.settings.docs_dir and __import__("pathlib").Path(documents.settings.docs_dir).resolve())
    else:
        assert r.status_code in (400, 422)


def test_unsupported_and_oversized_uploads_are_refused(world, monkeypatch):
    assert world.edhr.post(f"{P}/kbs/{world.kb_hr['id']}/documents", headers=world.eh, files={"file": ("run.exe", b"MZ")}).status_code in (400, 415, 422)
    import dataclasses
    from app.admin import documents_api
    monkeypatch.setattr(documents_api, "settings", dataclasses.replace(documents_api.settings, max_upload_mb=0))
    assert world.edhr.post(f"{P}/kbs/{world.kb_hr['id']}/documents", headers=world.eh, files={"file": ("big.txt", b"x" * 10)}).status_code == 413


def test_a_set_password_link_works_once_and_a_forged_one_never(world):
    u = users.create_user(world.db, "sec-link@sysfore.com", "link", "editor", world.hr)
    token, _ = users.issue_set_password_link(world.db, u["id"], "boss")
    anon = client()
    assert anon.post(P + "/auth/set-password", json={"token": token, "password": PW}).status_code == 200
    assert anon.post(P + "/auth/set-password", json={"token": token, "password": "another password 2"}).status_code in (400, 401, 410, 422)
    assert anon.post(P + "/auth/set-password", json={"token": token[:-3] + "xyz", "password": PW}).status_code in (400, 401, 410, 422)
    assert sign_in("sec-link@sysfore.com")[0].get(P + "/auth/me").status_code == 200


def test_login_does_not_reveal_whether_an_account_exists(world):
    a = client().post(P + "/auth/login", json={"email": "nobody-here@sysfore.com", "password": "whatever password"})
    b = client().post(P + "/auth/login", json={"email": "sec-ed-hr@sysfore.com", "password": "wrong password!!"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
