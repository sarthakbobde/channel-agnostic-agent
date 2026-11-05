"""Release 2, Phase 2: languages (detection, fallback, accepted list, answer language), the message catalogue and document
summaries, against the real application with real documents. The language detector is a stand-in (see conftest), except
in the retrieval check, which uses the real multilingual model when it has been downloaded."""
import csv
import re
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app import (catalog, chat_service, global_settings, guardrails, identity, language, llm, memory, prompts, rag, retriever,
                 scoped_settings)
from app.admin import users
from app.api import app
from app.channels import core
from app.db import (AnswerTrace, AuditLog, Department, DepartmentEditor, Document, GuardrailEvent, Message, MessageCatalogEntry,
                    ScopedSetting, UsageRecord, get_db)
from app.knowledge import doc_summary, documents, kbs
from tests.test_baseline import FakeMessage, FakeModel

P = "/admin/api"
PW = "a good password 1"
HINDI_DOC = "वार्षिक अवकाश नीति के अनुसार हर कर्मचारी को अठारह दिन की छुट्टी मिलती है। छुट्टी के लिए प्रबंधक से अनुमति लेनी होती है।"


class Fixed:
    """A detector that gives one answer for text containing a word, else defers to the script stand-in."""
    def __init__(self, rules, fallback):
        self.rules, self.fallback = rules, fallback

    def available(self):
        return True

    def detect(self, text):
        for word, result in self.rules.items():
            if word in (text or ""):
                return result
        return self.fallback.detect(text)


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


def wait(fn, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def world():
    db = get_db()
    kbs.ensure_default(db)
    prompts.seed(db)
    catalog.seed(db)
    global_settings.update({"summary_enabled": True}, "t", db)          # off by default; these tests are about summaries
    w = types.SimpleNamespace(db=db)
    w.guard0 = {k: getattr(guardrails.current(db), k) for k in ("enabled", "mask_enabled")}       # put back afterwards: the database is shared by every test module
    with db.session() as s:
        s.add(Department(name="MlHR", retention_days=30, banned_topics="", is_default=False))
        s.add(Department(name="MlIT", retention_days=30, banned_topics="", is_default=False))
    with db.session() as s:
        w.hr = s.scalar(select(Department.id).where(Department.name == "MlHR"))
        w.it = s.scalar(select(Department.id).where(Department.name == "MlIT"))
    users.create_admin(db, "ml-boss@sysfore.com", PW, domain="sysfore.com")
    w.ed_hr = person(db, "ml-ed-hr@sysfore.com", "editor", w.hr)
    person(db, "ml-asker@sysfore.com", "viewer", w.hr)
    with db.session() as s:
        s.add(DepartmentEditor(department_id=w.hr, user_id=w.ed_hr["id"]))
    w.admin, w.ah = sign_in("ml-boss@sysfore.com")
    w.edhr, w.eh = sign_in("ml-ed-hr@sysfore.com")
    w.kb_en = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Ml English", "department_id": w.hr}).json()
    w.kb_hi = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Ml Hindi", "department_id": w.hr}).json()
    w.kb_it = w.admin.post(P + "/kbs", headers=w.ah, json={"name": "Ml IT", "department_id": w.it}).json()

    def upload(kb, name, text):
        r = w.admin.post(f"{P}/kbs/{kb['id']}/documents", headers=w.ah, files={"file": (name, text.encode())})
        assert r.status_code == 201, r.text
        assert wait(lambda: documents.get(db, r.json()["id"])["status"] in ("Indexed", "Failed"), 90)
        return documents.get(db, r.json()["id"])
    w.upload = upload
    w.doc_en = upload(w.kb_en, "leave-policy.txt", "The annual leave policy gives every employee 18 days of leave each year.")
    w.doc_hi = upload(w.kb_hi, "avkash-niti.txt", HINDI_DOC)
    w.doc_it = upload(w.kb_it, "it-notes.txt", "The laptop refresh cycle is three years for every employee.")
    yield w
    guardrails.update(w.guard0, "t", db)
    guardrails.invalidate()
    documents.shutdown()
    doc_summary.shutdown()


@pytest.fixture(autouse=True)
def clean(world):
    for kb in (world.kb_en, world.kb_hi):          # the built-in MiniLM model cannot match across languages (see the spike)
        scoped_settings.put("kb", kb["id"], {"score_threshold": 0.0}, "t", world.db)
    yield
    with get_db().session() as s:
        s.execute(delete(ScopedSetting))
        s.execute(delete(MessageCatalogEntry).where(MessageCatalogEntry.lang != "en"))
    global_settings.update({"accepted_languages": [], "catalog_languages": []}, "t", world.db)
    scoped_settings.invalidate()
    catalog.invalidate()
    guardrails.update({"language_lists": {}, "unlisted_language": "english", "enabled": True, "mask_enabled": True}, "t", world.db)
    guardrails.invalidate()


def models(monkeypatch, answers, query="annual leave policy"):
    """Router and answer stand-ins. `answers` are returned one per answer call, in order."""
    router = FakeModel(reply='{"search": true, "query": "%s"}' % query)
    answerer = types.SimpleNamespace(calls=[], left=list(answers))

    def stream(msgs, **kw):
        answerer.calls.append(msgs)
        yield FakeMessage(answerer.left.pop(0) if len(answerer.left) > 1 else answerer.left[0])
    answerer.stream = stream
    monkeypatch.setattr(chat_service, "get_chat_model", lambda: router)
    monkeypatch.setattr(rag, "get_chat_model", lambda: answerer)
    return router, answerer


def ask(world, question, conv="web-ml-1", kb_ids=None, email="ml-asker@sysfore.com"):
    token = identity.bind(identity.resolve(email, world.db))
    try:
        return chat_service.chat(question, conv, kb_ids=kb_ids or [world.kb_en["id"]])
    finally:
        identity.unbind(token)


def approve_all(lang, prefix="X"):
    for key in catalog.KEYS:
        text = catalog.TEXTS[key][0]
        for ph in ("{seconds}", "{languages}", "{status}"):
            text = text if ph not in text else f"{prefix}-{lang}-{key} {ph}"
        catalog.save(key, lang, text if "{" in text else f"{prefix}-{lang}-{key}", "t")


def events(check, action=None, lang=None):
    with get_db().session() as s:
        q = select(func.coalesce(func.sum(GuardrailEvent.count), 0)).where(GuardrailEvent.check == check)
        if action:
            q = q.where(GuardrailEvent.action == action)
        if lang:
            q = q.where(GuardrailEvent.language == lang)
        return s.scalar(q)


# ---- normalization and detection -----------------------------------------------------------------------------------
def test_normalization_is_clean_up_only():
    assert language.normalize("  ｌｅａｖｅ​  policy \n\n\n\nnext  ") == "leave policy\n\nnext"
    assert language.normalize("क्ष") == "क्ष"                                   # the joiner inside Indic text is kept
    assert language.normalize("Leave POLICY") == "Leave POLICY"                  # case is never changed


def test_the_real_detector_runs_when_its_model_is_present():
    real = language.FastTextDetector(language.model_path())
    if not real.available():
        pytest.skip("the fastText model is not downloaded here")
    assert real.detect("How many days of annual leave do contractors get each year?")[0] == "en"
    assert real.detect("ठेकेदारों को हर साल कितने दिन की वार्षिक छुट्टी मिलती है?")[0] == "hi"


def test_unsure_detection_falls_back_to_the_conversation_then_the_default(world, monkeypatch):
    language.set_detector(Fixed({"ok": ("fr", 0.2)}, language.detector()))
    assert language.resolve("ok", last="hi", default="en").resolved == "hi"           # unsure: the conversation's language
    ctx = language.resolve("ok", last="", default="mr")
    assert (ctx.resolved, ctx.source, ctx.detected) == ("mr", "kb_default", "fr")
    assert language.resolve("", last="", default="en").resolved == "en"                # nothing to detect: never an error
    language.set_detector(language.Detector())                                          # no model at all: still answers
    models(monkeypatch, ["Eighteen days [1]."])
    assert ask(world, "annual leave?", conv="web-ml-nomodel")["language"] == "en"


# ---- answering in the question's language --------------------------------------------------------------------------
def test_hindi_question_about_an_english_policy_gets_a_hindi_answer_with_an_english_citation(world, monkeypatch):
    _, answerer = models(monkeypatch, ["वार्षिक अवकाश अठारह दिन का है [1]।"])
    r = ask(world, "वार्षिक छुट्टी कितने दिन की है?", conv="web-ml-hi")
    assert r["language"] == "hi" and r["found"] and r["sources"][0]["title"] == "leave-policy.txt"
    assert r["sources"][0]["doc_language"] == "en"
    system = answerer.calls[0][0].content
    assert "The user wrote in Hindi. Write your whole reply in Hindi" in system
    assert "(leave-policy.txt; language: English)" in system                         # passages labelled, never translated
    assert core.sources_line(r["sources"], r["language"]) == "leave-policy.txt [EN]"
    assert core.sources_line(r["sources"], "en") == "leave-policy.txt"
    with get_db().session() as s:
        assert s.scalar(select(Message.language).where(Message.conversation_id == "web-ml-hi", Message.role == "user")) == "hi"


def test_english_question_about_a_hindi_document_then_a_hindi_follow_up(world, monkeypatch):
    _, answerer = models(monkeypatch, ["Employees get eighteen days of leave each year [1].", "हर कर्मचारी को अठारह दिन [1]।"])
    # MiniLM scores this English-to-Hindi match below zero (see the spike), so search without a threshold here
    monkeypatch.setattr(rag, "search", lambda q, kb_ids=None: retriever.retrieve_scored(q, kb_ids=kb_ids))
    first = ask(world, "How many leave days do employees get?", conv="web-ml-mix", kb_ids=[world.kb_hi["id"]])
    assert first["language"] == "en" and first["sources"][0]["doc_language"] == "hi"
    assert "Write your whole reply in English" in answerer.calls[0][0].content
    second = ask(world, "और ठेकेदारों के लिए?", conv="web-ml-mix", kb_ids=[world.kb_hi["id"]])
    assert second["language"] == "hi" and "Write your whole reply in Hindi" in answerer.calls[1][0].content


def test_a_wrong_language_answer_is_retried_once_then_delivered_and_counted(world, monkeypatch):
    before_r, before_w = events("answer_language", "retried", "hi"), events("answer_language", "wrong", "hi")
    _, answerer = models(monkeypatch, ["Employees get eighteen days of annual leave [1].", "हर कर्मचारी को अठारह दिन की छुट्टी मिलती है [1]।"])
    r = ask(world, "वार्षिक छुट्टी कितने दिन की है?", conv="web-ml-retry1")
    assert len(answerer.calls) == 2 and "IMPORTANT: an earlier reply was not in Hindi" in answerer.calls[1][0].content
    assert r["answer"].startswith("हर कर्मचारी")
    _, answerer = models(monkeypatch, ["Employees get eighteen days of annual leave [1]."])
    r = ask(world, "वार्षिक छुट्टी कितने दिन की है?", conv="web-ml-retry2")
    assert len(answerer.calls) == 2 and r["answer"].startswith("Employees get")          # delivered, never an error
    assert events("answer_language", "retried", "hi") == before_r + 2 and events("answer_language", "wrong", "hi") == before_w + 1
    with get_db().session() as s:
        t = s.get(AnswerTrace, r["answer_id"])
        assert t.language == "hi" and t.language_detail["retried"] is True and t.language_detail["answer"] == "en"


def test_short_answers_and_not_found_are_never_retried(world, monkeypatch):
    _, answerer = models(monkeypatch, ["18 [1]"])
    ask(world, "वार्षिक छुट्टी कितने दिन की है?", conv="web-ml-short")
    _, answerer2 = models(monkeypatch, [prompts.NOT_FOUND_MARKER])
    r = ask(world, "वार्षिक छुट्टी कितने दिन की है?", conv="web-ml-nf")
    assert len(answerer.calls) == 1 and len(answerer2.calls) == 1 and r["found"] is False and r["sources"] == []


def test_hybrid_search_skips_keywords_for_a_cross_language_question(world, monkeypatch):
    scoped_settings.put("kb", world.kb_en["id"], {"retrieval_strategy": "hybrid"}, "t", world.db)
    used = []
    real = retriever._keyword_hits
    monkeypatch.setattr(retriever, "_keyword_hits", lambda *a, **k: used.append(1) or real(*a, **k))
    models(monkeypatch, ["वार्षिक अवकाश अठारह दिन का है [1]।"])
    ask(world, "वार्षिक छुट्टी कितने दिन की है?", conv="web-ml-hyb1")
    assert used == []
    models(monkeypatch, ["Eighteen days a year [1]."])
    ask(world, "How long is annual leave?", conv="web-ml-hyb2")
    assert used == [1]                                                                    # same-language questions still use keywords
    with get_db().session() as s:
        t = s.scalars(select(AnswerTrace).where(AnswerTrace.conversation_id == "web-ml-hyb1")).one()
        assert t.retrieval[0]["keywords_skipped"] is True and t.retrieval[0]["doc_languages"] == ["en"]


# ---- accepted languages (D39) ------------------------------------------------------------------------------------------
def test_a_language_cannot_be_accepted_until_its_messages_are_approved(world):
    with pytest.raises(ValueError, match="not all their system messages are approved"):
        global_settings.update({"accepted_languages": ["en", "hi"]}, "t", world.db)
    approve_all("hi")
    catalog.save("not_found", "hi", "यह ज्ञानकोष में नहीं मिला।", "t")
    global_settings.update({"accepted_languages": ["en", "hi"]}, "t", world.db)
    assert global_settings.current(world.db).accepted_languages == ("en", "hi")
    r = world.admin.put(P + "/settings", headers=world.ah, json={"values": {"accepted_languages": ["fr"]}})
    assert r.status_code == 422


def test_an_accepted_language_gets_its_own_system_texts(world, monkeypatch):
    approve_all("hi")
    catalog.save("not_found", "hi", "यह ज्ञानकोष में नहीं मिला।", "t")
    models(monkeypatch, [prompts.NOT_FOUND_MARKER])
    r = ask(world, "वार्षिक छुट्टी कितने दिन की है?", conv="web-ml-nfhi")
    assert r["answer"] == "यह ज्ञानकोष में नहीं मिला।"
    catalog.save("not_found", "mr", "Marathi draft", "t", status=catalog.DRAFT)       # a draft is never shown
    assert catalog.t("not_found", "mr") == catalog.TEXTS["not_found"][0]


def test_a_question_in_a_language_not_accepted_gets_the_polite_reply_and_nothing_else(world, monkeypatch):
    approve_all("hi")
    global_settings.update({"accepted_languages": ["en", "hi"]}, "t", world.db)
    language.set_detector(Fixed({"Combien": ("fr", 0.97)}, language.detector()))
    router, answerer = models(monkeypatch, ["never"])
    monkeypatch.setattr(rag, "search", lambda *a, **k: pytest.fail("must not search"))
    with get_db().session() as s:
        usage_before = s.scalar(select(func.count()).select_from(UsageRecord))
    rejected = events("language", "rejected", "fr")
    r = ask(world, "Combien de jours de congé annuel ?", conv="web-ml-fr")
    assert r["rejected"] == "language" and r["answer_id"] is None and r["sources"] == []
    assert r["answer"] == "Sorry, I can only answer questions in English, हिन्दी."
    assert router.calls == [] and answerer.calls == []
    with get_db().session() as s:
        assert s.scalar(select(func.count()).select_from(UsageRecord)) == usage_before
        assert s.scalar(select(func.count()).select_from(Message).where(Message.conversation_id == "web-ml-fr")) == 0
        assert s.scalar(select(func.count()).select_from(AnswerTrace).where(AnswerTrace.conversation_id == "web-ml-fr")) == 0
    assert events("language", "rejected", "fr") == rejected + 1


def test_with_no_list_nothing_is_rejected(world, monkeypatch):
    language.set_detector(Fixed({"Combien": ("fr", 0.97)}, language.detector()))
    models(monkeypatch, ["Dix-huit jours [1]."])
    r = ask(world, "Combien de jours de congé annuel ?", conv="web-ml-fr2")
    assert r.get("rejected") is None and r["language"] == "fr" and r["answer_id"]


def test_unsure_detection_falls_back_first_then_meets_the_list(world, monkeypatch):
    approve_all("hi")
    global_settings.update({"accepted_languages": ["en", "hi"]}, "t", world.db)
    language.set_detector(Fixed({"Combien": ("fr", 0.3)}, language.detector()))       # unsure French
    models(monkeypatch, ["Eighteen days [1]."])
    r = ask(world, "Combien de jours ?", conv="web-ml-fb")
    assert r.get("rejected") is None and r["language"] == "en"                          # fell back to the default, which is accepted
    scoped_settings.put("kb", world.kb_en["id"], {"default_language": "fr"}, "t", world.db)
    r = ask(world, "Combien de jours ?", conv="web-ml-fb2")
    assert r["rejected"] == "language"                                                   # the fallback is checked too


def test_a_kb_that_does_not_accept_the_language_is_not_searched(world, monkeypatch):
    approve_all("hi")
    scoped_settings.put("kb", world.kb_hi["id"], {"accepted_languages": ["hi"]}, "t", world.db)
    searched = []
    real = retriever._search_kb
    monkeypatch.setattr(retriever, "_search_kb", lambda kb, *a, **k: searched.append(kb["id"]) or real(kb, *a, **k))
    models(monkeypatch, ["Eighteen days [1]."])
    r = ask(world, "How long is annual leave?", conv="web-ml-multi", kb_ids=[world.kb_en["id"], world.kb_hi["id"]])
    assert searched == [world.kb_en["id"]] and r["kb_ids"] == [world.kb_en["id"]]
    r = ask(world, "How long is annual leave?", conv="web-ml-multi2", kb_ids=[world.kb_hi["id"]])
    assert r["rejected"] == "language" and r["answer"] == "Sorry, I can only answer questions in हिन्दी."


# ---- message catalogue -------------------------------------------------------------------------------------------------
def test_english_system_texts_are_byte_identical(world):
    from app.api import PROVIDER_REFUSED_REPLY
    from app.channels.gchat import TIMEOUT_REPLY
    assert catalog.t("not_found", "en") == rag.FALLBACK == "I couldn't find this in the knowledge base."
    assert catalog.t("blocked", "en") == guardrails.current().blocked_reply
    assert catalog.t("friendly_error", "hi") == guardrails.FRIENDLY_ERROR
    assert catalog.t("error", "en") == core.ERROR_REPLY and catalog.t("slow_down", "") == core.SLOW_DOWN
    assert catalog.t("denied", "en") == identity.DENIED_REPLY
    assert catalog.t("provider_refused", "en") == PROVIDER_REFUSED_REPLY and catalog.t("gchat_timeout", "en") == TIMEOUT_REPLY
    assert catalog.t("slow_down_wait", "en", seconds=7) == "You are asking too quickly. Please wait 7 seconds and try again."


def test_catalogue_screen_drafts_with_ai_and_approves(world, monkeypatch):
    r = world.admin.post(P + "/catalog/hi/draft", headers=world.ah)
    assert r.status_code == 422                                                           # add the language first
    global_settings.update({"catalog_languages": ["hi"]}, "t", world.db)
    monkeypatch.setattr(llm, "get_chat_model", lambda: FakeModel(reply="हिंदी पाठ {seconds} {languages} {status}"))
    r = world.admin.post(P + "/catalog/hi/draft", headers=world.ah)
    assert r.status_code == 200 and r.json()["drafted"] == len(catalog.KEYS)
    assert catalog.t("not_found", "hi") == catalog.TEXTS["not_found"][0]                # drafts are not used
    r = world.admin.post(P + "/catalog/not_found/hi/approve", headers=world.ah)
    assert r.status_code == 200 and catalog.t("not_found", "hi").startswith("हिंदी पाठ")
    r = world.admin.put(P + "/catalog/slow_down_wait/hi", headers=world.ah, json={"text": "धीरे"})
    assert r.status_code == 422 and "{seconds}" in r.json()["detail"]                     # placeholders must stay
    r = world.admin.put(P + "/catalog/blocked/en", headers=world.ah, json={"text": "x"})
    assert r.status_code == 422                                                           # English blocked text lives on Guardrails
    with get_db().session() as s:
        acts = set(s.scalars(select(AuditLog.action).where(AuditLog.action.like("catalog.%"))))
        assert {"catalog.draft", "catalog.approve"} <= acts and not any("हिंदी" in (d or "") for d in s.scalars(select(AuditLog.detail)))


def test_chat_app_refusals_are_sent_in_the_persons_language(world):
    approve_all("hi")
    catalog.save("denied", "hi", "आपकी पहुँच बंद है।", "t")
    from app.channels.base import Envelope
    ctx = world.db
    u = users.create_user(ctx, "ml-gone@sysfore.com", "gone", "viewer", world.hr)
    with ctx.session() as s:
        from app.db import User
        s.get(User, u["id"]).status = "Deactivated"
    r = core.handle(Envelope("slack", "t", "c", "c", "u", "मेरी छुट्टी?", (), "ml-gone@sysfore.com"))
    assert r["answer"] == "आपकी पहुँच बंद है।" and r["language"] == "hi"


# ---- guardrails: masking on its own switch (D36) and checks per language -----------------------------------------------
def test_masking_follows_its_own_switch_not_the_master_switch(world):
    guardrails.update({"enabled": False}, "t", world.db)
    guardrails.invalidate()
    gate = guardrails.mask_input("write to rita.k@example.net please")
    assert "rita.k@example.net" not in gate.text and "<EMAIL_ADDRESS>" in gate.text
    guardrails.update({"mask_enabled": False}, "t", world.db)
    guardrails.invalidate()
    assert guardrails.mask_input("write to rita.k@example.net").text == "write to rita.k@example.net"


def test_word_lists_per_language_and_the_choice_for_a_language_without_lists(world, monkeypatch):
    guardrails.update({"language_lists": {"hi": {"injection": [], "toxicity": [], "banned_topics": ["वेतन"]}}}, "t", world.db)
    guardrails.invalidate()
    approve_all("hi")
    catalog.save("blocked", "hi", "मैं इसमें मदद नहीं कर सकता।", "t")
    router, answerer = models(monkeypatch, ["never"])
    r = ask(world, "मेरा वेतन कितना है?", conv="web-ml-ban")
    assert r["blocked"] and r["answer"] == "मैं इसमें मदद नहीं कर सकता।" and router.calls == []
    language.set_detector(Fixed({"Combien": ("fr", 0.97)}, language.detector()))
    before = events("language_checks", "english", "fr")
    models(monkeypatch, ["Dix-huit jours [1]."])
    assert not ask(world, "Combien de jours ?", conv="web-ml-unl").get("blocked")       # default: English checks, with a warning
    assert events("language_checks", "english", "fr") == before + 1
    guardrails.update({"unlisted_language": "block"}, "t", world.db)
    guardrails.invalidate()
    assert ask(world, "Combien de jours ?", conv="web-ml-unl2")["blocked"]
    with pytest.raises(ValueError):
        guardrails.validate({"language_lists": {"en": {"injection": ["x"]}}})


# ---- ingestion and back-fill -------------------------------------------------------------------------------------------
def test_passages_and_documents_get_their_language_and_old_ones_are_back_filled(world):
    store = kbs.store_for(world.kb_hi)
    meta = store.get([f"{world.doc_hi['doc_key']}::0"])[0]["metadata"]
    assert (meta["lang"], meta["doc_lang"]) == ("hi", "hi") and documents.get(world.db, world.doc_hi["id"])["language"] == "hi"
    # a passage indexed before Phase 2: its metadata has no language keys at all
    with world.db.session() as s:
        old = Document(kb_id=world.kb_hi["id"], name="old.txt", doc_key="doc-old-ml", status="Indexed", chunks=1, uploaded_by="t")
        s.add(old)
        s.flush()
        old_id = old.id
    old_meta = {k: v for k, v in {**meta, "doc_id": "doc-old-ml", "source": "old.txt"}.items() if k not in ("lang", "lang_confidence", "doc_lang")}
    store.add(["doc-old-ml::0"], [HINDI_DOC], [old_meta])
    assert documents.backfill_languages(world.db, world.kb_hi["id"]) == 1
    got = store.get(["doc-old-ml::0"])[0]
    assert got["metadata"]["lang"] == "hi" and got["metadata"]["doc_lang"] == "hi" and got["text"] == HINDI_DOC   # text untouched
    assert documents.get(world.db, old_id)["language"] == "hi"
    assert documents.backfill_languages(world.db, world.kb_hi["id"]) == 0                 # idempotent
    store.delete_by_document("doc-old-ml")
    with world.db.session() as s:
        s.delete(s.get(Document, old_id))


# ---- trace -----------------------------------------------------------------------------------------------------------
def test_the_trace_records_the_language_and_the_snapshot_follows_it(world, monkeypatch):
    models(monkeypatch, ["Eighteen days [1]."])
    a = ask(world, "How long is annual leave?", conv="web-ml-tr1")
    models(monkeypatch, ["अठारह दिन [1]।"])
    b = ask(world, "वार्षिक छुट्टी कितनी है?", conv="web-ml-tr2")
    with get_db().session() as s:
        ta, tb = s.get(AnswerTrace, a["answer_id"]), s.get(AnswerTrace, b["answer_id"])
        assert (ta.language, tb.language) == ("en", "hi") and tb.language_detail["source"] == "detected"
        assert ta.config_snapshot_id != tb.config_snapshot_id
        assert tb.language_detail["doc_languages"] == {str(world.kb_en["id"]): ["en"]}


# ---- document summaries (D46) --------------------------------------------------------------------------------------------
def test_a_new_document_is_indexed_without_waiting_for_its_summary(world, monkeypatch):
    gate = threading.Event()

    class Slow(FakeModel):
        def invoke(self, msgs, **kw):
            self.calls.append(("invoke", msgs))
            gate.wait(20)
            return FakeMessage("A short policy on parking for employees.")
    model = Slow()
    monkeypatch.setattr(llm, "get_chat_model", lambda: model)
    d = world.upload(world.kb_en, "parking.txt", "Parking is free for employees on Fridays in the north lot.")
    assert d["status"] == "Indexed" and documents.get(world.db, d["id"])["summary_status"] == "Pending"
    rows = world.admin.get(f"{P}/kbs/{world.kb_en['id']}/documents", headers=world.ah).json()
    row = next(x for x in (rows if isinstance(rows, list) else rows.get("documents", [])) if x["id"] == d["id"])
    assert row["summary_status"] == "Pending"
    gate.set()
    assert wait(lambda: documents.get(world.db, d["id"])["summary_status"] == "Ready")
    got = documents.get(world.db, d["id"])
    assert got["summary"] == "A short policy on parking for employees." and got["summary_lang"] == "en"
    assert "Summarise this document in 2 to 3 sentences, in English" in model.calls[0][1][0].content


def test_a_hindi_document_gets_a_hindi_summary_and_a_failure_can_be_regenerated(world, monkeypatch):
    def broken():
        raise RuntimeError("model down")
    monkeypatch.setattr(llm, "get_chat_model", broken)
    assert doc_summary.schedule(world.db, world.doc_hi["id"], force=True)
    assert wait(lambda: documents.get(world.db, world.doc_hi["id"])["summary_status"] == "Failed")
    assert documents.get(world.db, world.doc_hi["id"])["status"] == "Indexed"
    model = FakeModel(reply="यह वार्षिक अवकाश की नीति है।")
    monkeypatch.setattr(llm, "get_chat_model", lambda: model)
    r = world.edhr.post(f"{P}/documents/{world.doc_hi['id']}/summary", headers=world.eh)
    assert r.status_code == 200
    assert wait(lambda: documents.get(world.db, world.doc_hi["id"])["summary_status"] == "Ready")
    assert documents.get(world.db, world.doc_hi["id"])["summary_lang"] == "hi"
    assert "in Hindi" in model.calls[0][1][0].content


def test_reindexing_an_unchanged_file_makes_no_model_call_and_replacing_it_does(world, monkeypatch):
    model = FakeModel(reply="Laptops are replaced every three years.")
    monkeypatch.setattr(llm, "get_chat_model", lambda: model)
    doc_summary.schedule(world.db, world.doc_it["id"], force=True)
    assert wait(lambda: documents.get(world.db, world.doc_it["id"])["summary_status"] == "Ready")
    n = len(model.calls)
    documents.reindex(world.db, world.doc_it["id"])
    assert wait(lambda: documents.get(world.db, world.doc_it["id"])["status"] == "Indexed")
    time.sleep(1.5)
    assert len(model.calls) == n and documents.get(world.db, world.doc_it["id"])["summary_status"] == "Ready"
    model.reply = "Laptops are now replaced every four years."
    documents.replace(world.db, world.doc_it["id"], "it-notes.txt", b"The laptop refresh cycle is four years now.")
    assert wait(lambda: documents.get(world.db, world.doc_it["id"])["summary"] == "Laptops are now replaced every four years.")
    assert len(model.calls) > n


def test_summaries_are_display_only_and_follow_the_document(world, monkeypatch):
    with world.db.session() as s:
        s.get(Document, world.doc_en["id"]).summary = "SUMMARYMARK-4471"
    _, answerer = models(monkeypatch, ["Eighteen days [1]."])
    router = chat_service.get_chat_model()
    ask(world, "How long is annual leave?", conv="web-ml-sum")
    texts = [m.content for msgs in answerer.calls for m in msgs] + [m.content for _, msgs in router.calls for m in msgs]
    assert texts and not any("SUMMARYMARK-4471" in t for t in texts)
    assert world.edhr.get(f"{P}/kbs/{world.kb_it['id']}/documents", headers=world.eh).status_code == 404   # other department
    d = world.upload(world.kb_en, "temp.txt", "A temporary note about desks.")
    documents.delete(world.db, d["id"])
    with world.db.session() as s:
        assert s.get(Document, d["id"]) is None


def test_summarising_existing_documents_shows_an_estimate_first_and_is_admin_only(world, monkeypatch):
    model = FakeModel(reply="Summary.")
    monkeypatch.setattr(llm, "get_chat_model", lambda: model)
    e = world.admin.get(f"{P}/summaries/estimate?kb_id={world.kb_en['id']}", headers=world.ah)
    assert e.status_code == 200 and set(e.json()) >= {"documents", "input_tokens", "cost", "note"}
    assert model.calls == []                                                              # the estimate sends nothing
    monkeypatch.setattr(doc_summary, "BACKFILL_PAUSE", 0)
    r = world.admin.post(f"{P}/summaries/backfill", headers=world.ah, json={"kb_id": world.kb_en["id"]})
    assert r.status_code == 200
    assert wait(lambda: all(d["summary_status"] == "Ready" for d in documents.list_for_kb(world.db, world.kb_en["id"])))


# ---- roles ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("method,path", [("GET", "/catalog"), ("PUT", "/catalog/not_found/hi"), ("POST", "/catalog/not_found/hi/approve"),
                                         ("POST", "/catalog/hi/draft"), ("GET", "/reports/languages"), ("GET", "/summaries/estimate"),
                                         ("POST", "/summaries/backfill")])
def test_editors_are_refused_the_admin_only_language_routes(world, method, path):
    r = world.edhr.request(method, P + path, headers=world.eh, json={"text": "x"} if method == "PUT" else {})
    assert r.status_code == 403


def test_an_editor_cannot_regenerate_another_departments_summary_and_anonymous_is_refused(world):
    assert world.edhr.post(f"{P}/documents/{world.doc_it['id']}/summary", headers=world.eh).status_code == 404
    anon = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000))
    assert anon.get(P + "/catalog").status_code == 401
    assert world.admin.post(f"{P}/documents/{world.doc_en['id']}/summary").status_code == 403     # no CSRF token


def test_the_language_report(world, monkeypatch):
    models(monkeypatch, ["Eighteen days [1]."])
    ask(world, "How long is annual leave?", conv="web-ml-rep")
    r = world.admin.get(P + "/reports/languages", headers=world.ah).json()
    en = next(x for x in r["rows"] if x["code"] == "en")
    assert en["answers"] >= 1 and r["detector"] == "ready"


# ---- retrieval check (a test aid, not a product feature) --------------------------------------------------------------
TARGETS = {"en-en": 0.9, "hi-en": 0.9, "mr-en": 0.8}          # from the Phase 2 spike (DECISIONS.md)


def _sections(text):
    names = sorted({row["expected_section"] for row in csv.DictReader(open(Path(__file__).parent / "data" / "retrieval_check.csv", encoding="utf-8"))})
    marks = sorted((m.start(), s) for s in names for m in [re.search(r"^\s*" + re.escape(s), text, re.M)] if m)
    return marks


def test_retrieval_check_recall_at_5_per_language_pair():
    from app.knowledge.embeddings import LocalMultilingualEmbeddings
    if not LocalMultilingualEmbeddings.available():
        pytest.skip("the multilingual model is not downloaded here")
    import numpy as np
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from pypdf import PdfReader
    pdf = Path(__file__).parent.parent / "data" / "docs" / "HR-Policy.pdf"
    text = "\n".join(p.extract_text() or "" for p in PdfReader(str(pdf)).pages)
    marks = _sections(text)
    sp = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=150)
    chunks, labels = [], []
    for i, (start, sec) in enumerate(marks):
        for c in sp.split_text(text[start:marks[i + 1][0] if i + 1 < len(marks) else len(text)]):
            chunks.append(c)
            labels.append(sec)
    model = LocalMultilingualEmbeddings()
    vectors = np.array(model.embed_documents(chunks))
    rows = list(csv.DictReader(open(Path(__file__).parent / "data" / "retrieval_check.csv", encoding="utf-8")))
    recall = {}
    for pair in TARGETS:
        qs = [r for r in rows if r["pair"] == pair]
        hits = sum(r["expected_section"] in {labels[i] for i in np.argsort(-(vectors @ np.array(model.embed_query(r["question"]))))[:5]} for r in qs)
        recall[pair] = hits / len(qs)
    print("recall@5", recall)
    assert all(recall[p] >= TARGETS[p] for p in TARGETS), recall


def test_the_recommended_threshold_is_offered_for_the_model_in_use(world):
    from app.knowledge import embeddings
    assert embeddings.LocalMultilingualEmbeddings.RECOMMENDED_THRESHOLD == 0.75
    k = next(x for x in world.admin.get(P + "/kbs", headers=world.ah).json()["kbs"] if x["id"] == world.kb_en["id"])
    assert (k["recommended_threshold"], k["threshold"]) == (0.35, 0.0)
    e = world.admin.get(f"{P}/kbs/{world.kb_en['id']}/index/estimate", headers=world.ah).json()
    assert e["recommended_threshold"] == 0.35


def test_an_unsure_detection_of_an_unaccepted_language_falls_back_instead_of_turning_the_question_away(world, monkeypatch):
    """Found on the real policies: a Hindi question was detected as Marathi at 0.54. With English and Hindi accepted it must
    not get the "not supported" reply; only a sure detection (>= 0.8) of an unaccepted language does."""
    approve_all("hi")
    global_settings.update({"accepted_languages": ["en", "hi"]}, "t", world.db)
    scoped_settings.put("kb", world.kb_en["id"], {"default_language": "hi"}, "t", world.db)
    language.set_detector(Fixed({"वापस": ("mr", 0.54), "परत": ("mr", 0.95)}, language.detector()))
    models(monkeypatch, ["कंपनी पहले से सूचना देगी [1]।"])
    r = ask(world, "अगर कंपनी ऑफिस वापस बुलाए तो क्या होगा?", conv="web-ml-mr1")
    assert r.get("rejected") is None and r["language"] == "hi"
    r = ask(world, "कंपनीने ऑफिसला परत बोलावले तर काय?", conv="web-ml-mr2")
    assert r["rejected"] == "language"
