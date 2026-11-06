"""Release 3, Phase 1: feedback capture (D49, D50, D61, D62), against the real application, real documents and the real Chroma store.
The models are stand-ins; the channel presenters are checked on the payloads each platform receives."""
import pathlib
import re
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app import chat_service, feedback, global_settings, guardrails, identity, memory, prompts, rag, scoped_settings, suggestions
from app.admin import users
from app.channels import core
from app.channels.base import Envelope
from app.db import Department, Feedback, Message, ScopedSetting, get_db
from app.knowledge import documents, kbs
from tests.test_baseline import FakeModel
from tests.test_release2_suggestions import KEY, PW, P, person, sign_in

CONV = "web-1-fb"


@pytest.fixture(scope="module")
def world():
    db = get_db()
    kbs.ensure_default(db)
    prompts.seed(db)
    w = types.SimpleNamespace(db=db)
    with db.session() as s:
        s.add(Department(name="FbHR", retention_days=30, banned_topics="", is_default=False))
    with db.session() as s:
        w.hr = s.scalar(select(Department.id).where(Department.name == "FbHR"))
    users.create_admin(db, "fb-boss@sysfore.com", PW, domain="sysfore.com")
    w.asker = person(db, "fb-asker@sysfore.com", "viewer", w.hr)
    w.other = person(db, "fb-other@sysfore.com", "viewer", w.hr)
    w.admin, w.ah = sign_in("fb-boss@sysfore.com")
    w.kb = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Fb HR", "department_id": w.hr}).json()
    r = w.admin.post(f"{P}/kbs/{w.kb['id']}/documents", headers=w.ah, files={"file": ("leave.txt", b"Unused annual leave can be carried over, up to five days.")})
    assert r.status_code == 201, r.text
    end = time.time() + 90
    while time.time() < end and documents.get(db, r.json()["id"])["status"] not in ("Indexed", "Failed"):
        time.sleep(0.2)
    yield w
    documents.shutdown()


@pytest.fixture(autouse=True)
def setup(world, monkeypatch):
    monkeypatch.setattr(suggestions, "env", types.SimpleNamespace(secret_key=KEY))
    scoped_settings.put("channel", "web", {"feedback_enabled": True}, "t", world.db)
    scoped_settings.put("kb", world.kb["id"], {"feedback_enabled": True, "score_threshold": 0.3}, "t", world.db)
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: FakeModel(reply='{"search": true, "query": "annual leave carry over"}'))
    monkeypatch.setattr(rag, "get_chat_model", lambda: FakeModel(parts=["Up to five days can be carried over [1]."]))
    before = guardrails.current(world.db).mask_enabled
    yield
    with get_db().session() as s:
        s.execute(delete(ScopedSetting))
        s.execute(delete(Feedback))
    scoped_settings.invalidate()
    global_settings.update({"feedback_comment_max_chars": 1000, "feedback_payload_days": 14, "feedback_masking": "always"}, "t", world.db)
    guardrails.update({"mask_enabled": before}, "t", world.db)
    guardrails.invalidate()


def ask(world, conv=CONV, email="fb-asker@sysfore.com"):
    token = identity.bind(identity.resolve(email, world.db))
    try:
        return chat_service.chat("Can I carry over my leave?", conv, kb_ids=[world.kb["id"]], asker="web:" + email)
    finally:
        identity.unbind(token)


def key(email="fb-asker@sysfore.com"):
    return suggestions.asker_key("web:" + email)


def rate(world, result, rating, comment="", attachments=None, email="fb-asker@sysfore.com"):
    data = feedback.verify(result["feedback"]["payload"], key(email))
    return feedback.capture(data, rating, comment, attachments, who=identity.resolve(email, world.db), db=world.db)


def records():
    with get_db().session() as s:
        return list(s.scalars(select(Feedback).order_by(Feedback.id)))


# ---- the controls --------------------------------------------------------------------------------------------------------------
def test_an_answer_carries_signed_rating_controls_when_feedback_is_on(world):
    r = ask(world)
    ctl = r["feedback"]
    assert ctl["answer_id"] == r["answer_id"] and ctl["payload"] and "reasons" not in ctl
    assert ctl["labels"]["up"] == "Helpful" and ctl["labels"]["link"].startswith("Link to a screenshot") and "reason" not in ctl["labels"]
    assert feedback.is_rating(ctl["payload"])


def test_no_controls_when_feedback_is_off_for_the_channel_or_the_knowledge_base(world):
    scoped_settings.put("channel", "web", {"feedback_enabled": False}, "t", world.db)
    assert ask(world, conv="web-1-fb-off")["feedback"] is None
    scoped_settings.put("channel", "web", {"feedback_enabled": True}, "t", world.db)
    scoped_settings.put("kb", world.kb["id"], {"feedback_enabled": False}, "t", world.db)
    assert ask(world, conv="web-1-fb-off2")["feedback"] is None


def test_feedback_is_off_by_default():
    assert global_settings.current().feedback_enabled is False


def test_the_rating_controls_are_not_stored_with_the_answer(world):
    r = ask(world, conv="web-1-fb-meta")
    with get_db().session() as s:
        assert "feedback" not in (s.get(Message, r["answer_id"]).meta or {})


# ---- capture -------------------------------------------------------------------------------------------------------------------
def test_a_positive_rating_with_no_comment_ends_at_once(world):
    out = rate(world, ask(world, conv="web-1-fb-up"), "up")
    assert out["created"] and out["state"] == feedback.NO_TICKET and out["intent"] == "positive"
    [row] = records()
    assert (row.rating, row.state, row.intent, row.intent_source, row.channel, row.kb_id) == ("up", "no_ticket", "positive", "rating", "web", world.kb["id"])
    assert row.answer_id and row.user_id == world.asker["id"] and row.reason == ""


def test_a_thumbs_down_waits_for_processing(world):
    out = rate(world, ask(world, conv="web-1-fb-down"), "down")
    assert out["state"] == feedback.CAPTURED and out["intent"] is None


def test_a_positive_rating_with_a_comment_is_not_closed(world):
    out = rate(world, ask(world, conv="web-1-fb-upc"), "up", comment="Thanks, but the second paragraph is wrong")
    assert out["state"] == feedback.CAPTURED and out["intent"] is None


def test_rating_again_updates_the_same_record(world):
    r = ask(world, conv="web-1-fb-again")
    first = rate(world, r, "down")
    second = rate(world, r, "down", comment="It says five days but the policy says ten")
    third = rate(world, r, "down", comment="It says five days but the policy says ten")      # a repeated click
    assert first["id"] == second["id"] == third["id"] and first["created"] and not second["created"]
    assert second["changed"] and not third["changed"]
    [row] = records()
    assert "policy says ten" in row.comment
    flipped = rate(world, r, "up")                                    # a thumbs-up afterwards: still the same record, and the comment keeps it open
    assert flipped["id"] == first["id"] and records()[0].rating == "up" and flipped["state"] == feedback.CAPTURED


def test_a_rating_after_the_record_was_ticketed_is_a_new_record(world):
    r = ask(world, conv="web-1-fb-ticketed")
    first = rate(world, r, "down")
    with get_db().session() as s:
        s.get(Feedback, first["id"]).state = feedback.TICKETED
    second = rate(world, r, "down", comment="Still wrong")
    assert second["created"] and second["id"] != first["id"]
    assert [x.state for x in records()] == ["ticketed", "captured"]


@pytest.mark.parametrize("state", [feedback.HUMAN_REVIEW, feedback.READY_FOR_TICKET])
def test_a_review_or_a_feedback_about_to_be_ticketed_is_not_undone_by_a_later_click(world, state):
    r = ask(world, conv="web-1-fb-" + state)
    first = rate(world, r, "down")
    with get_db().session() as s:
        s.get(Feedback, first["id"]).state = state
    again = rate(world, r, "up")
    assert again["state"] == state and again["id"] == first["id"] and not again["created"]      # ready_for_ticket is open too (D49)


def test_two_open_records_for_one_rater_and_answer_cannot_exist(world):
    r = ask(world, conv="web-1-fb-unique")
    first = rate(world, r, "down")
    with pytest.raises(IntegrityError):
        with get_db().session() as s:
            s.add(Feedback(answer_id=r["answer_id"], conversation_id=CONV, rater_key=key(), rating="up", state=feedback.CAPTURED, attachments=[]))
    assert [x.id for x in records()] == [first["id"]]


def test_two_clicks_at_once_end_as_one_record(world, monkeypatch):
    """The second click looks for an open record before the first one is stored, finds none, and is refused by the database: it is then
    stored as an update of the first."""
    r = ask(world, conv="web-1-fb-race")
    data = feedback.verify(r["feedback"]["payload"], key())
    real, calls = feedback._open_row, {"n": 0}

    def blind_once(s, d):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(s, d)
    feedback.capture(data, "down", "", None, who=identity.resolve("fb-asker@sysfore.com", world.db), db=world.db)
    monkeypatch.setattr(feedback, "_open_row", blind_once)
    out = feedback.capture(data, "down", "Wrong figure", None, who=identity.resolve("fb-asker@sysfore.com", world.db), db=world.db)
    assert calls["n"] == 2 and not out["created"] and len(records()) == 1 and records()[0].comment == "Wrong figure"


def test_the_comment_is_masked_before_it_is_stored(world):
    guardrails.update({"mask_enabled": True}, "t", world.db)
    guardrails.invalidate()
    rate(world, ask(world, conv="web-1-fb-mask"), "down", comment="Call me on rita.k@example.net, my card is 4111 1111 1111 1111")
    [row] = records()
    assert "rita.k@example.net" not in row.comment and "4111" not in row.comment


def test_a_comment_is_masked_even_when_question_masking_is_off(world):
    guardrails.update({"mask_enabled": False}, "t", world.db)
    guardrails.invalidate()
    rate(world, ask(world, conv="web-1-fb-maskoff"), "down", comment="Write to rita.k@example.net about it")
    assert "rita.k@example.net" not in records()[0].comment


def test_an_admin_can_make_comments_follow_the_masking_switch(world):
    global_settings.update({"feedback_masking": "follow_switch"}, "t", world.db)
    guardrails.update({"mask_enabled": False}, "t", world.db)
    guardrails.invalidate()
    rate(world, ask(world, conv="web-1-fb-follow"), "down", comment="Write to rita.k@example.net about it")
    assert "rita.k@example.net" in records()[0].comment                 # documented: with the switch off, the text is stored as typed
    guardrails.update({"mask_enabled": True}, "t", world.db)
    guardrails.invalidate()
    rate(world, ask(world, conv="web-1-fb-follow2"), "down", comment="Write to rita.k@example.net about it")
    assert "rita.k@example.net" not in records()[1].comment
    with pytest.raises(ValueError):
        global_settings.update({"feedback_masking": "sometimes"}, "t", world.db)


def test_a_comment_that_cannot_be_masked_is_refused_not_stored(world, monkeypatch):
    r = ask(world, conv="web-1-fb-failmask")
    monkeypatch.setattr(guardrails, "mask_with", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("engine down")))
    with pytest.raises(RuntimeError):
        rate(world, r, "down", comment="Write to rita.k@example.net")
    assert records() == []


def test_a_long_comment_is_cut_to_the_limit(world):
    global_settings.update({"feedback_comment_max_chars": 50}, "t", world.db)
    rate(world, ask(world, conv="web-1-fb-long"), "down", comment="word " * 100)
    assert len(records()[0].comment) <= 50


def test_attachments_are_kept_as_links_only(world):
    rate(world, ask(world, conv="web-1-fb-links"), "down",
         attachments=["https://example.com/screenshot.png", "file:///C:/secret.txt", "javascript:alert(1)", "not a link", "https://example.com/screenshot.png"])
    assert records()[0].attachments == ["https://example.com/screenshot.png"]


def test_an_unknown_rating_is_refused(world):
    r = ask(world, conv="web-1-fb-bad")
    with pytest.raises(ValueError):
        rate(world, r, "meh")
    assert records() == []


def test_there_is_no_reason_list(world):
    assert "feedback_reasons" not in {f["name"] for f in global_settings.SPEC}
    assert not any(k.startswith("feedback_reason") for k in __import__("app.catalog", fromlist=["TEXTS"]).TEXTS)


# ---- who may rate ---------------------------------------------------------------------------------------------------------------
def test_only_the_person_who_asked_can_rate(world):
    r = ask(world, conv="web-1-fb-owner")
    with pytest.raises(suggestions.PayloadError) as e:
        feedback.verify(r["feedback"]["payload"], key("fb-other@sysfore.com"))
    assert e.value.key == "feedback_not_yours"


def test_a_forged_tampered_or_expired_payload_is_rejected(world):
    r = ask(world, conv="web-1-fb-forge")
    payload = r["feedback"]["payload"]
    body, sig = payload.split(".")
    for bad in (payload[:-3] + "AAA", body + "." + sig[::-1], "x." + sig, "", "nonsense"):
        with pytest.raises(suggestions.PayloadError) as e:
            feedback.verify(bad, key())
        assert e.value.key == "feedback_invalid"
    old = feedback.sign(r["answer_id"], key(), datetime.now(timezone.utc) - timedelta(seconds=5))
    with pytest.raises(suggestions.PayloadError) as e:
        feedback.verify(old, key())
    assert e.value.key == "feedback_expired"
    suggestion_payload = suggestions.sign(r["answer_id"], 1, 1, "x", key(), datetime.now(timezone.utc) + timedelta(days=1))
    assert not feedback.is_rating(suggestion_payload)
    with pytest.raises(suggestions.PayloadError):
        feedback.verify(suggestion_payload, key())                       # a suggestion button is not a rating button


def test_a_rating_payload_does_not_pass_as_a_suggestion(world):
    r = ask(world, conv="web-1-fb-cross")
    with pytest.raises(suggestions.PayloadError):
        suggestions.verify(r["feedback"]["payload"], key())


# ---- through the chat apps' entry point ----------------------------------------------------------------------------------------------
def envelope(email="fb-asker@sysfore.com"):
    return Envelope("web", "1", "fb-app", "t1", "u1", "", (), email)


def test_a_click_through_the_core_stores_the_rating_and_answers_with_thanks(world, monkeypatch):
    monkeypatch.setattr(core, "asker_of", lambda env: "web:" + env.email)
    r = ask(world, conv="web-1-fb-core")
    out = core.handle_action(envelope(), r["feedback"]["payload"], rating="down")
    assert out["answer"] == "Thank you for your feedback." and out["feedback"]["state"] == "captured"
    form = out["feedback_form"]
    assert form["payload"] == r["feedback"]["payload"] and "reasons" not in form and form["labels"]["comment"] and form["labels"]["link"]
    out2 = core.handle_action(envelope(), r["feedback"]["payload"], rating="down", comment="It is in the policy", attachments=["https://example.com/a.png", "not a link"])
    assert out2["answer"] == "Thank you, your feedback was updated." and "feedback_form" not in out2
    [row] = records()
    assert row.comment == "It is in the policy" and row.attachments == ["https://example.com/a.png"]
    up = core.handle_action(envelope(), ask(world, conv="web-1-fb-core2")["feedback"]["payload"], rating="up")
    assert "feedback_form" not in up


def test_a_click_by_someone_else_is_refused_and_stores_nothing(world, monkeypatch):
    monkeypatch.setattr(core, "asker_of", lambda env: "web:" + env.email)
    r = ask(world, conv="web-1-fb-core3")
    out = core.handle_action(envelope("fb-other@sysfore.com"), r["feedback"]["payload"], rating="up")
    assert out["answer"] == "Only the person who asked can rate this answer." and records() == []


def test_a_failing_write_gives_an_apology_and_never_raises(world, monkeypatch):
    monkeypatch.setattr(core, "asker_of", lambda env: "web:" + env.email)
    r = ask(world, conv="web-1-fb-core4")
    monkeypatch.setattr(feedback, "capture", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database down")))
    out = core.handle_action(envelope(), r["feedback"]["payload"], rating="up")
    assert out["answer"].startswith("Sorry, something went wrong")


def test_a_deactivated_person_cannot_rate(world, monkeypatch):
    monkeypatch.setattr(core, "asker_of", lambda env: "web:" + env.email)
    r = ask(world, conv="web-1-fb-core5")
    users.update_user(world.db, world.asker["id"], status="Deactivated")
    try:
        out = core.handle_action(envelope(), r["feedback"]["payload"], rating="up")
        assert "turned off" in out["answer"] and records() == []
    finally:
        users.update_user(world.db, world.asker["id"], status="Active")


# ---- deleting a conversation (D62) --------------------------------------------------------------------------------------------------
def test_deleting_a_conversation_keeps_its_feedback_and_its_answer_readable_for_review(world):
    r = ask(world, conv="web-1-fb-del")
    rate(world, r, "down", comment="Wrong")
    other = ask(world, conv="web-1-fb-keep")
    rate(world, other, "up")
    assert memory.delete("web-1-fb-del")
    assert len(records()) == 2                                           # nothing is purged
    deleted = [x for x in records() if x.answer_id == r["answer_id"]][0]
    assert deleted.conversation_id != "web-1-fb-del" and "web-1-fb-del" in deleted.conversation_id
    assert memory.get_messages("web-1-fb-del") == [] and "web-1-fb-del" not in [c["id"] for c in memory.list_conversations()]
    with get_db().session() as s:
        assert s.get(Message, r["answer_id"]).content and s.get(Message, r["answer_id"]).conversation_id == deleted.conversation_id
    assert world.admin.get(f"{P}/traces/{r['answer_id']}").status_code == 200       # the review screens still reach it
    again = ask(world, conv="web-1-fb-del")                              # the same id can start a fresh conversation
    assert again["answer_id"] != r["answer_id"] and memory.get_messages("web-1-fb-del")


def test_the_retention_job_moves_a_conversation_aside_through_the_same_helper(world):
    r = ask(world, conv="web-1-fb-shared")
    rate(world, r, "down")
    with get_db().session() as s:
        assert memory.move_aside(s, ["web-1-fb-shared"]) >= 2
    assert len(records()) == 1 and records()[0].conversation_id != "web-1-fb-shared"


def test_every_table_with_a_conversation_id_is_moved_aside_with_the_conversation():
    from app.db import Base
    have = {t.__tablename__ for t in memory.CONVERSATION_TABLES}
    found = {t.name for t in Base.metadata.tables.values() if "conversation_id" in t.c}
    assert found == have, f"a table keeps rows per conversation but is not moved aside with it: {sorted(found - have)}"


def test_no_code_deletes_conversations_without_the_helper():
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    bad = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bdelete\((Conversation|Message|AnswerTrace|Suggestion|Feedback)\b", text) or re.search(r"s\.delete\((c|conv|conversation)\)", text):
            bad.append(str(path.relative_to(root)))
    assert bad == [], f"conversations must be deleted through memory.move_aside, not directly: {bad}"


# ---- the Web API ----------------------------------------------------------------------------------------------------------------------
def web(email, monkeypatch, world):
    from fastapi.testclient import TestClient
    from app.api import app
    c = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), raise_server_exceptions=False)
    r = c.post("/chat-auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    monkeypatch.setattr("app.chat_auth.web_kb_ids", lambda: [world.kb["id"]])
    return c, {"Authorization": "Bearer " + r.json()["token"]}


def test_the_web_api_carries_the_controls_and_takes_a_rating_by_payload(world, monkeypatch):
    c, h = web("fb-asker@sysfore.com", monkeypatch, world)
    r = c.post("/chat", headers=h, json={"message": "Can I carry over my leave?", "conv_id": "api-fb"}).json()
    ctl = r["feedback"]
    assert ctl and ctl["payload"] and ctl["labels"]["down"] == "Not helpful"
    c2, h2 = web("fb-other@sysfore.com", monkeypatch, world)
    assert c2.post("/chat/feedback", headers=h2, json={"conv_id": "api-fb", "payload": ctl["payload"], "rating": "up"}).status_code == 403
    down = c.post("/chat/feedback", headers=h, json={"conv_id": "api-fb", "payload": ctl["payload"], "rating": "down"}).json()
    assert down["message"] == "Thank you for your feedback." and down["state"] == "captured" and "reasons" not in down["form"]
    again = c.post("/chat/feedback", headers=h, json={"conv_id": "api-fb", "payload": ctl["payload"], "rating": "down", "comment": "Wrong",
                                                       "attachments": ["https://example.com/a.png"]}).json()
    assert again["message"] == "Thank you, your feedback was updated." and again["form"] is None
    assert len(records()) == 1 and records()[0].comment == "Wrong" and records()[0].attachments == ["https://example.com/a.png"]
    assert c.post("/chat/feedback", headers=h, json={"conv_id": "api-fb", "payload": ctl["payload"], "rating": "meh"}).status_code == 400
    assert c.post("/chat/feedback", headers=h, json={"conv_id": "api-fb", "payload": "junk", "rating": "up"}).status_code == 400


# ---- what each chat app shows and sends --------------------------------------------------------------------------------------------
def result_with_controls(world):
    r = ask(world, conv="web-1-fb-pres")
    return {**r, "sources": [], "suggestions": []}


def test_slack_shows_thumbs_and_opens_a_form_for_a_thumbs_down(world):
    from app.channels.slack import SlackPresenter
    r = result_with_controls(world)
    out = SlackPresenter().render(r, True)
    [row] = [b for b in out["blocks"] if b.get("block_id") == "caa_rating"]
    assert [e["action_id"] for e in row["elements"]] == ["caa_rate_up", "caa_rate_down"] and row["elements"][0]["value"] == r["feedback"]["payload"]
    assert SlackPresenter().render({**r, "feedback": None}, True) == {"text": r["answer"]}            # no blocks when rating is off
    view = SlackPresenter.form_view(feedback.form_for("p", 1), {"p": "p"})
    assert view["callback_id"] == "caa_rate_form" and [b["block_id"] for b in view["blocks"]] == ["comment", "link"]


def test_teams_shows_thumbs_and_a_form_card(world):
    from app.channels.teams import TeamsPresenter
    r = result_with_controls(world)
    card = TeamsPresenter().render(r, True).attachments[0].content
    assert [(a["title"], a["data"]["caa_rating"]) for a in card["actions"]] == [("\U0001F44D Helpful", "up"), ("\U0001F44E Not helpful", "down")]
    assert all(a["data"]["caa_action"] == r["feedback"]["payload"] for a in card["actions"])
    assert TeamsPresenter().render({**r, "feedback": None}, True) == r["answer"]
    form = TeamsPresenter().form(feedback.form_for("p", 1)).attachments[0].content
    assert [i["id"] for i in form["body"][1:]] == ["comment", "link"] and form["actions"][0]["data"] == {"caa_action": "p", "caa_rating": "down"}


def test_google_chat_shows_thumbs_and_reads_a_click_and_a_form(world):
    from app.channels.gchat import GChatAdapter, GChatPresenter
    r = result_with_controls(world)
    text, cards = GChatPresenter().render(r, True)
    rating = [c for c in cards if c["cardId"] == "caa-rating"][0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"]
    assert [b["onClick"]["action"]["function"] for b in rating] == ["caa_rate", "caa_rate"]
    payload = r["feedback"]["payload"]
    classic = {"type": "CARD_CLICKED", "action": {"actionMethodName": "caa_rate", "parameters": [{"key": "payload", "value": payload}, {"key": "rating", "value": "down"}]}}
    assert GChatAdapter.click_payload(classic) == payload and GChatAdapter.click_fields(classic) == {"rating": "down", "comment": ""}
    addon = {"commonEventObject": {"invokedFunction": "caa_rate_form", "parameters": {"payload": payload, "rating": "down"},
                                   "formInputs": {"comment": {"stringInputs": {"value": ["Wrong policy"]}}, "link": {"stringInputs": {"value": ["https://example.com/shot.png"]}}}}}
    assert GChatAdapter.click_payload(addon) == payload
    assert GChatAdapter.click_fields(addon) == {"rating": "down", "comment": "Wrong policy", "attachments": ["https://example.com/shot.png"]}
    assert GChatAdapter.click_fields({"commonEventObject": {"invokedFunction": "caa_suggest", "parameters": {"payload": "x"}}}) == {}
    form = GChatPresenter.form_card(feedback.form_for(payload, 1))
    widgets = form["card"]["sections"][0]["widgets"]
    assert "textInput" in widgets[0] and "textInput" in widgets[1]
    assert widgets[2]["buttonList"]["buttons"][0]["onClick"]["action"]["function"] == "caa_rate_form"
    _, cards_off = GChatPresenter(buttons=False).render({**r, "feedback_form": feedback.form_for(payload, 1)}, True)
    assert cards_off == []                                                  # buttons off: no rating card and no form either


# ---- the admin screens -------------------------------------------------------------------------------------------------------------
def test_admin_sees_all_feedback_and_an_editor_only_their_departments(world):
    from app.db import DepartmentEditor
    with world.db.session() as s:
        s.add(Department(name="FbIT", retention_days=30, banned_topics="", is_default=False))
    with world.db.session() as s:
        other_dept = s.scalar(select(Department.id).where(Department.name == "FbIT"))
    ed = person(world.db, "fb-ed@sysfore.com", "editor", world.hr)
    with world.db.session() as s:
        s.add(DepartmentEditor(department_id=world.hr, user_id=ed["id"]))
    kb2 = world.admin.post(P + "/kbs", headers=world.ah, json={"name": "Fb IT", "department_id": other_dept}).json()
    rate(world, ask(world, conv="web-1-fb-adm1"), "down", comment="Wrong")
    with get_db().session() as s:
        s.add(Feedback(answer_id=999, conversation_id="web-1-elsewhere", rater_key="z", kb_id=kb2["id"], channel="slack", rating="up", state="no_ticket", attachments=[]))
    editor, _ = sign_in("fb-ed@sysfore.com")
    mine = editor.get(P + "/feedback").json()
    assert mine["total"] == 1 and mine["items"][0]["kb"] == "Fb HR" and mine["items"][0]["comment"] == "Wrong" and "reasons" not in mine and "reason" not in mine["items"][0]
    assert world.admin.get(P + "/feedback").json()["total"] == 2
    assert editor.get(P + "/feedback", params={"kb_id": kb2["id"]}).json()["total"] == 0               # another department's knowledge base
    rep = editor.get(P + "/feedback/report").json()
    assert (rep["total"], rep["up"], rep["down"]) == (1, 0, 1) and "by_reason" not in rep
    full = world.admin.get(P + "/feedback/report").json()
    assert {r["name"] for r in full["by_channel"]} == {"web", "slack"} and full["total"] == 2
    assert editor.get(P + "/feedback", params={"state": "nonsense"}).status_code == 422
    assert editor.get(P + "/feedback", params={"rating": "meh"}).status_code == 422


def test_a_viewer_and_a_signed_out_visitor_cannot_open_the_feedback_api(world):
    from fastapi.testclient import TestClient
    from app.api import app
    anon = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000), raise_server_exceptions=False)
    assert anon.get(P + "/feedback").status_code == 401
    r = anon.post(P + "/auth/login", json={"email": "fb-asker@sysfore.com", "password": PW})
    assert r.status_code in (401, 403)                                  # a Viewer has no panel at all


def test_the_feedback_screen_is_listed_for_admins_and_editors_only():
    from app.admin import roles
    assert "feedback" in roles.screens_for("admin") and "feedback" in roles.screens_for("editor") and roles.screens_for("viewer") == []
    assert roles.allowed("editor", "/admin/api/feedback") and not roles.allowed("viewer", "/admin/api/feedback")


def test_the_texts_of_the_rating_controls():
    labels = feedback.labels()
    assert labels["link"].startswith("Link to a screenshot") and labels["thanks"] == "Thank you for your feedback." and "reason" not in labels
