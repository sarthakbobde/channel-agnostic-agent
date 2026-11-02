"""Release 2, Phase 4: suggested follow-up questions (D26, D37, D40, D47), against the real application, real documents and
the real Chroma store. The models are stand-ins; the presenters are checked on the payloads each platform receives."""
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import chat_service, global_settings, guardrails, identity, memory, prompts, rag, scoped_settings, suggestions
from app.admin import users
from app.api import app
from app.channels import core
from app.channels.base import Envelope
from app.channels.gchat import GChatAdapter, GChatPresenter
from app.channels.slack import SlackPresenter
from app.channels.teams import TeamsPresenter
from app.db import Department, DepartmentEditor, GuardrailEvent, Message, ScopedSetting, Suggestion, get_db
from app.knowledge import documents, kbs
from tests.test_baseline import FakeMessage, FakeModel

P = "/admin/api"
PW = "a good password 1"
KEY = "x" * 43 + "="
BLOCK = "\n<<<SUGGESTIONS>>>\n{}\n<<<END>>>"
GOOD = ["How often are laptops refreshed?", "Is parking free on Fridays?", "Can unused leave be carried over?"]


def sign_in(email):
    c = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), raise_server_exceptions=False)
    r = c.post(P + "/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf"]}


def person(db, email, role, department_id):
    u = users.create_user(db, email, email.split("@")[0], role, department_id)
    token, _ = users.issue_set_password_link(db, u["id"], "setup")
    users.redeem_set_password_link(db, token, PW)
    return u


@pytest.fixture(scope="module")
def world():
    db = get_db()
    kbs.ensure_default(db)
    prompts.seed(db)
    w = types.SimpleNamespace(db=db)
    with db.session() as s:
        s.add(Department(name="SgHR", retention_days=30, banned_topics="salary", is_default=False))
    with db.session() as s:
        w.hr = s.scalar(select(Department.id).where(Department.name == "SgHR"))
    users.create_admin(db, "sg-boss@sysfore.com", PW, domain="sysfore.com")
    ed = person(db, "sg-ed@sysfore.com", "editor", w.hr)
    w.asker = person(db, "sg-asker@sysfore.com", "viewer", w.hr)
    w.other = person(db, "sg-other@sysfore.com", "viewer", w.hr)
    with db.session() as s:
        s.add(DepartmentEditor(department_id=w.hr, user_id=ed["id"]))
    w.admin, w.ah = sign_in("sg-boss@sysfore.com")
    w.ed, w.eh = sign_in("sg-ed@sysfore.com")
    w.kb = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Sg HR", "department_id": w.hr}).json()

    def upload(name, text, **form):
        r = w.admin.post(f"{P}/kbs/{w.kb['id']}/documents", headers=w.ah, files={"file": (name, text.encode())}, data=form)
        assert r.status_code == 201, r.text
        end = time.time() + 90
        while time.time() < end and documents.get(db, r.json()["id"])["status"] not in ("Indexed", "Failed"):
            time.sleep(0.2)
        return documents.get(db, r.json()["id"])
    upload("leave.txt", "Unused annual leave can be carried over to the next year, up to five days.")
    upload("parking.txt", "Parking in the north lot is free for staff on Fridays.")
    upload("laptops.txt", "Laptops are refreshed every three years by the IT desk.")
    upload("board.txt", "The board bonus pool is forty percent of profit.", visibility="people", user_ids=str(w.other["id"]))
    yield w
    documents.shutdown()


@pytest.fixture(autouse=True)
def setup(world, monkeypatch):
    monkeypatch.setattr(suggestions, "env", types.SimpleNamespace(secret_key=KEY))
    scoped_settings.put("channel", "web", {"suggestions_enabled": True}, "t", world.db)
    scoped_settings.put("kb", world.kb["id"], {"suggestions_enabled": True, "score_threshold": 0.3}, "t", world.db)
    yield
    with get_db().session() as s:
        s.execute(delete(ScopedSetting))
        s.execute(delete(Suggestion))
    scoped_settings.invalidate()
    global_settings.update({"suggestions_mode": "same_call", "suggestions_max_searches": 3, "suggestions_payload_days": 14}, "t", world.db)
    suggestions._cache.clear()


def models(monkeypatch, answer, candidates=None, block=True):
    router = FakeModel(reply='{"search": true, "query": "annual leave carry over"}')
    text = answer + (BLOCK.format("\n".join(candidates or [])) if block else "")
    answerer = FakeModel(parts=[text])
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: router)
    monkeypatch.setattr(rag, "get_chat_model", lambda: answerer)
    return router, answerer


def ask(world, question="Can I carry over my leave?", conv="web-1-sg", email="sg-asker@sysfore.com", asker=""):
    token = identity.bind(identity.resolve(email, world.db))
    try:
        return chat_service.chat(question, conv, kb_ids=[world.kb["id"]], asker=asker or "web:" + email)
    finally:
        identity.unbind(token)


def rows(conv):
    with get_db().session() as s:
        return [(r.position, r.text, r.outcome, r.support) for r in s.scalars(select(Suggestion).where(Suggestion.conversation_id == conv)
                                                                              .order_by(Suggestion.position))]


def no_block_count():
    with get_db().session() as s:
        return s.scalar(select(func.coalesce(func.sum(GuardrailEvent.count), 0)).where(GuardrailEvent.check == "suggestions"))


# ---- format and parsing ----------------------------------------------------------------------------------------------
def test_the_parser_splits_the_block_and_never_lets_it_into_the_answer():
    assert suggestions.parse("Answer [1]." + BLOCK.format("1. First?\n- Second?\n\nThird?\nFourth?"), 3) == ("Answer [1].", ["First?", "Second?", "Third?"], True)
    answer, found, ok = suggestions.parse("Answer.\n<<<SUGGESTIONS>>>\nHalf a block", 3)
    assert answer == "Answer." and not ok
    assert suggestions.parse("Just an answer.", 3) == ("Just an answer.", [], False)


def test_the_format_is_asked_for_only_when_switched_on_and_the_answer_never_shows_the_block(world, monkeypatch):
    _, answerer = models(monkeypatch, "You can carry over five days [1].", GOOD)
    r = ask(world, conv="web-1-fmt")
    system = answerer.calls[0][1][0].content
    assert "# FOLLOW-UP QUESTIONS (set by the system)" in system and "<<<SUGGESTIONS>>>" not in r["answer"]
    assert r["answer"] == "You can carry over five days [1]."
    with get_db().session() as s:
        stored = s.scalar(select(Message.content).where(Message.id == r["answer_id"]))
    assert "<<<" not in stored
    scoped_settings.put("channel", "web", {"suggestions_enabled": False}, "t", world.db)
    _, answerer = models(monkeypatch, "Five days [1].")
    r = ask(world, conv="web-1-fmt2")
    assert "FOLLOW-UP QUESTIONS" not in answerer.calls[0][1][0].content and r["suggestions"] == []


def test_a_missing_block_shows_the_answer_alone_and_counts(world, monkeypatch):
    before = no_block_count()
    models(monkeypatch, "You can carry over five days [1].", block=False)
    r = ask(world, conv="web-1-noblock")
    assert r["answer"] == "You can carry over five days [1]." and r["suggestions"] == [] and rows("web-1-noblock") == []
    assert no_block_count() == before + 1


# ---- the three checks ----------------------------------------------------------------------------------------------------
def test_the_two_best_supported_are_shown_and_every_candidate_is_stored(world, monkeypatch):
    models(monkeypatch, "Five days [1].", GOOD)
    r = ask(world, conv="web-1-best")
    stored = rows("web-1-best")
    assert [o for _, _, o, _ in stored].count("Shown") == 2 and [o for _, _, o, _ in stored].count("Not selected") == 1
    shown = sorted((x for x in stored if x[2] == "Shown"), key=lambda x: -x[3])
    lowest = min(x[3] for x in stored)
    assert all(x[3] > lowest for x in shown)                                    # the least supported is the one dropped
    assert [s["text"] for s in r["suggestions"]] == [x[1] for x in shown] and all(s["payload"] and s["suggestion_id"] for s in r["suggestions"])


def test_no_shown_suggestion_is_unanswerable_and_none_leaks_a_restricted_document(world, monkeypatch):
    models(monkeypatch, "Five days [1].", ["What is on the cafeteria menu today?", "How big is the board bonus pool?"])
    r = ask(world, conv="web-1-na")
    assert r["suggestions"] == [] and {o for _, _, o, _ in rows("web-1-na")} == {"Not answerable"}
    models(monkeypatch, "Five days [1].", ["How big is the board bonus pool?"])
    r = ask(world, conv="web-1-na2", email="sg-other@sysfore.com")                   # the person who may see it
    assert [s["text"] for s in r["suggestions"]] == ["How big is the board bonus pool?"]


def test_repeats_exact_or_in_other_words_are_dropped(world, monkeypatch):
    models(monkeypatch, "Five days [1].", ["can i CARRY over my leave", "Can I carry over my leave to next year?", "Is parking free on Fridays?"])
    global_settings.update({"suggestions_dedup_threshold": 0.85}, "t", world.db)
    r = ask(world, "Can I carry over my leave?", conv="web-1-dup")
    outcomes = {t: o for _, t, o, _ in rows("web-1-dup")}
    assert outcomes["can i CARRY over my leave"] == "Duplicate"                              # exact, after normalization
    assert outcomes["Can I carry over my leave to next year?"] == "Duplicate"                 # same meaning
    assert [s["text"] for s in r["suggestions"]] == ["Is parking free on Fridays?"]
    models(monkeypatch, "Fridays [1].", ["Is parking free on Fridays?", "How often are laptops refreshed?"])
    r = ask(world, "Where can I park?", conv="web-1-dup")                                      # already shown in this conversation
    assert [s["text"] for s in r["suggestions"]] == ["How often are laptops refreshed?"]
    global_settings.update({"suggestions_dedup_threshold": 0.9}, "t", world.db)


def test_candidates_that_fail_the_checks_are_dropped(world, monkeypatch):
    models(monkeypatch, "Five days [1].", ["Can I email rita.k@example.net about carrying over leave?", "Is parking free on Fridays?"])
    r = ask(world, conv="web-1-checks")
    outcomes = {pos: o for pos, _, o, _ in rows("web-1-checks")}
    assert outcomes[1] == "Failed checks"
    assert [s["text"] for s in r["suggestions"]] == ["Is parking free on Fridays?"]
    assert all("rita.k@example.net" not in t for _, t, _, _ in rows("web-1-checks"))          # stored masked


def test_a_ceiling_on_checking_searches(world, monkeypatch):
    global_settings.update({"suggestions_max_searches": 1}, "t", world.db)
    models(monkeypatch, "Five days [1].", GOOD)
    ask(world, conv="web-1-ceil")
    assert [o for _, _, o, _ in rows("web-1-ceil")] == ["Shown", "Not selected", "Not selected"]


def test_a_failing_storage_write_still_delivers_the_answer_and_its_suggestions(world, monkeypatch):
    class Down:
        def session(self):
            raise RuntimeError("db down")
    real = suggestions.store
    monkeypatch.setattr(suggestions, "store", lambda turn, rows, asker, db=None: real(turn, rows, asker, db=Down()))
    models(monkeypatch, "Five days [1].", GOOD)
    r = ask(world, conv="web-1-broken")
    assert r["answer"] == "Five days [1]." and len(r["suggestions"]) == 2 and all(s["suggestion_id"] is None and s["payload"] for s in r["suggestions"])


def test_toggling_the_kb_takes_effect_on_the_next_answer(world, monkeypatch):
    scoped_settings.put("kb", world.kb["id"], {"suggestions_enabled": False}, "t", world.db)
    models(monkeypatch, "Five days [1].", GOOD)
    assert ask(world, conv="web-1-tog")["suggestions"] == []
    scoped_settings.put("kb", world.kb["id"], {"suggestions_enabled": True}, "t", world.db)
    models(monkeypatch, "Five days [1].", GOOD)
    assert len(ask(world, conv="web-1-tog")["suggestions"]) == 2


def test_second_call_mode(world, monkeypatch):
    global_settings.update({"suggestions_mode": "second_call"}, "t", world.db)
    router, answerer = models(monkeypatch, "Five days [1].", block=False)
    second = FakeModel(reply=BLOCK.format("\n".join(GOOD)))
    from app import llm
    monkeypatch.setattr(llm, "get_chat_model", lambda: second)
    r = ask(world, conv="web-1-second")
    assert "FOLLOW-UP QUESTIONS" not in answerer.calls[0][1][0].content
    assert len(second.calls) == 1 and "CONTEXT:" in second.calls[0][1][0].content and len(r["suggestions"]) == 2


# ---- history, clicks and payloads ----------------------------------------------------------------------------------------
def test_reopening_shows_the_same_buttons_until_they_expire(world, monkeypatch):
    models(monkeypatch, "Five days [1].", GOOD)
    r = ask(world, conv="web-1-hist")
    history = memory.get_messages("web-1-hist")
    answer = next(m for m in history if m["id"] == r["answer_id"])
    assert [s["text"] for s in answer["suggestions"]] == [s["text"] for s in r["suggestions"]] and all(s["payload"] for s in answer["suggestions"])
    with get_db().session() as s:
        for row in s.scalars(select(Suggestion).where(Suggestion.conversation_id == "web-1-hist")):
            row.shown_at = datetime.now(timezone.utc) - timedelta(days=15)
    answer = next(m for m in memory.get_messages("web-1-hist") if m["id"] == r["answer_id"])
    assert answer["suggestions"] and all(s["payload"] is None for s in answer["suggestions"])        # plain text now


def test_a_click_asks_in_the_same_conversation_once_and_only_for_the_asker(world, monkeypatch):
    env = Envelope("slack", "T1", "C1", "171.1", "U1", "Can I carry over my leave?", (world.kb["id"],), "sg-asker@sysfore.com")
    scoped_settings.put("channel", "slack", {"suggestions_enabled": True}, "t", world.db)
    models(monkeypatch, "Five days [1].", GOOD)
    first = core.handle(env)
    chip = first["suggestions"][0]
    models(monkeypatch, "Every three years [1].", GOOD)
    result = core.handle_action(env, chip["payload"])
    assert result["answer"] == "Every three years [1]."
    with get_db().session() as s:
        row = s.get(Suggestion, chip["suggestion_id"])
        question = s.get(Message, row.clicked_question_id)
        assert row.clicked_at is not None and question.content == chip["text"] and question.conversation_id == env.conv_id
    assert core.handle_action(env, chip["payload"]).get("ignored") is True                      # a replay changes nothing
    stranger = Envelope("slack", "T1", "C1", "171.1", "U2", "", (world.kb["id"],), "sg-other@sysfore.com")
    assert core.handle_action(stranger, first["suggestions"][1]["payload"])["answer"] == "Only the person who asked can use these suggestions."
    forged = chip["payload"][:-4] + ("AAAA" if not chip["payload"].endswith("AAAA") else "BBBB")
    assert core.handle_action(env, forged)["answer"] == "This button is not valid."
    old = suggestions.sign(first["answer_id"], None, 1, "x", suggestions.asker_key(core.asker_of(env)), datetime.now(timezone.utc) - timedelta(seconds=1))
    assert core.handle_action(env, old)["answer"] == "This suggestion has expired. Type the question instead."


def test_payloads_survive_key_rotation(monkeypatch):
    monkeypatch.setattr(suggestions, "env", types.SimpleNamespace(secret_key="old" + KEY))
    p = suggestions.sign(1, 2, 1, "Q?", "u", datetime.now(timezone.utc) + timedelta(days=1))
    monkeypatch.setattr(suggestions, "env", types.SimpleNamespace(secret_key="new" + KEY + ",old" + KEY))
    assert suggestions.verify(p, "u")["t"] == "Q?"
    monkeypatch.setattr(suggestions, "env", types.SimpleNamespace(secret_key="new" + KEY))
    with pytest.raises(suggestions.PayloadError):
        suggestions.verify(p, "u")


def test_the_web_api_click(world, monkeypatch):
    def token(email):
        c = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), raise_server_exceptions=False)
        r = c.post("/chat-auth/login", json={"email": email, "password": PW})
        assert r.status_code == 200, r.text
        return c, {"Authorization": "Bearer " + r.json()["token"]}
    c, h = token("sg-asker@sysfore.com")
    monkeypatch.setattr("app.chat_auth.web_kb_ids", lambda: [world.kb["id"]])
    models(monkeypatch, "Five days [1].", GOOD)
    r = c.post("/chat", headers=h, json={"message": "Can I carry over my leave?", "conv_id": "api-sg"}).json()
    chip = r["suggestions"][0]
    c2, h2 = token("sg-other@sysfore.com")
    assert c2.post("/chat/action", headers=h2, json={"conv_id": "api-sg", "payload": chip["payload"]}).status_code == 403
    models(monkeypatch, "Every three years [1].", GOOD)
    r2 = c.post("/chat/action", headers=h, json={"conv_id": "api-sg", "payload": chip["payload"]})
    assert r2.status_code == 200 and r2.json()["answer"] == "Every three years [1]."
    assert c.post("/chat/action", headers=h, json={"conv_id": "api-sg", "payload": chip["payload"]}).status_code == 409
    convs = c.get("/conversations", headers=h).json()
    assert any(x["id"] == "api-sg" for x in convs)
    msgs = c.get("/conversations/api-sg", headers=h).json()["messages"]
    assert any(m.get("suggestions") for m in msgs if m["role"] == "assistant")


def test_deleting_the_conversation_deletes_its_suggestions(world, monkeypatch):
    models(monkeypatch, "Five days [1].", GOOD)
    ask(world, conv="web-1-del")
    assert rows("web-1-del")
    memory.delete("web-1-del")
    assert rows("web-1-del") == []


# ---- presenters ------------------------------------------------------------------------------------------------------------
RESULT = {"answer": "**Five** days [1].", "sources": [{"source": "leave.txt", "page": None}], "language": "en",
          "suggestions": [{"text": "Is parking free on Fridays?", "payload": "P1.sig"}, {"text": "Old one?", "payload": None}]}


def test_slack_buttons():
    out = SlackPresenter().render(RESULT, True)
    buttons = out["blocks"][1]["elements"]
    assert [b["value"] for b in buttons] == ["P1.sig"] and buttons[0]["action_id"] == "caa_suggest_0"
    assert "• Old one?" in out["text"] and "*Five* days" in out["text"]
    assert "blocks" not in SlackPresenter().render({**RESULT, "suggestions": []}, True)


def test_teams_adaptive_card():
    activity = TeamsPresenter().render(RESULT, True)
    card = activity.attachments[0].content
    assert activity.attachments[0].content_type == "application/vnd.microsoft.card.adaptive"
    assert card["actions"] == [{"type": "Action.Submit", "title": "Is parking free on Fridays?", "data": {"caa_action": "P1.sig"}}]
    assert TeamsPresenter().render({**RESULT, "suggestions": []}, False) == "**Five** days [1]."


def test_google_chat_buttons_and_clicks_in_both_formats():
    text, cards = GChatPresenter().render(RESULT, True)
    button = cards[0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"][0]
    assert button["onClick"]["action"] == {"function": "caa_suggest", "parameters": [{"key": "payload", "value": "P1.sig"}]}
    assert GChatPresenter(buttons=False).render(RESULT, True)[1] == []                       # text only
    classic = {"type": "CARD_CLICKED", "action": {"actionMethodName": "caa_suggest", "parameters": [{"key": "payload", "value": "P1.sig"}]},
               "space": {"name": "spaces/A"}, "message": {"thread": {"name": "spaces/A/threads/T"}}, "user": {"name": "users/1"}}
    addon = {"commonEventObject": {"invokedFunction": "caa_suggest", "parameters": {"payload": "P1.sig"}},
             "chat": {"user": {"name": "users/1"}, "buttonClickedPayload": {"space": {"name": "spaces/A"}, "message": {"thread": {"name": "spaces/A/threads/T"}}}}}
    for body, is_addon in ((classic, False), (addon, True)):
        assert GChatAdapter.click_payload(body) == "P1.sig"
        assert GChatAdapter._parse(body)[:3] == ("click", "spaces/A", "spaces/A/threads/T") and GChatAdapter._parse(body)[5] is is_addon
    assert GChatAdapter._reply("hi", False, cards, click=True)["actionResponse"] == {"type": "NEW_MESSAGE"}
    assert GChatAdapter._reply("hi", True, cards)["hostAppDataAction"]["chatDataAction"]["createMessageAction"]["message"]["cardsV2"] == cards


# ---- settings, report and roles ------------------------------------------------------------------------------------------
def test_editors_toggle_their_own_kbs_but_not_channels_and_the_report_is_admin_only(world, monkeypatch):
    assert world.ed.put(f"{P}/scoped-settings/kb/{world.kb['id']}", headers=world.eh, json={"values": {"suggestions_enabled": True}}).status_code == 200
    assert world.ed.put(f"{P}/scoped-settings/channel/web", headers=world.eh, json={"values": {"suggestions_enabled": True}}).status_code == 403
    assert world.ed.get(P + "/reports/suggestions", headers=world.eh).status_code == 403
    assert "web" in world.admin.get(P + "/scoped-settings/scopes", headers=world.ah).json()["channels"]
    models(monkeypatch, "Five days [1].", GOOD)
    ask(world, conv="web-1-rep")
    r = world.admin.get(P + "/reports/suggestions", headers=world.ah).json()
    assert any(x["channel"] == "web" and x["shown"] >= 2 for x in r["rows"])
    assert {o["name"] for o in r["outcomes"]} >= {"Shown", "Not selected"}
