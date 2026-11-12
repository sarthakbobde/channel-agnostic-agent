"""Replay verification (Release 3, Phase 3; D53): ask the question again and judge whether the problem still shows.

1. `possible()` runs first and calls no model. It answers "not possible" when the answer cannot be reproduced fairly: its trace is gone, a
   cited document was deleted or changed after the answer, the rater can no longer see a cited document, a pinned prompt version or the
   knowledge base is gone. (Release 2 dropped the document version and the allow-list signature from the trace; this uses the document's
   update time and a fresh access check instead.)
2. `replay()` runs the normal answer pipeline in replay mode (chat_service.chat_stream): the same prompt versions as the trace, the rater's
   CURRENT access and CURRENT settings, nothing saved (no message, no trace, no suggestion), no rate limit, usage under `fb_replay`.
3. `judge()` compares the replayed answer with the issue and says confirmed, failed or inconclusive. The model's reply is checked in code.

What is stored is masked text only, and never a passage: the replayed answer (masked), the outcome, a short reason, and which settings
differ from the original answer's (model, retrieval, glossary), so a reviewer can see WHY an answer changed.
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select

from app import agent_runtime, chat_service, global_settings, identity, memory, prompts, ticketing
from app.db import AnswerTrace, Database, Document, Feedback, KnowledgeBase, Message, Prompt, User, get_db
from app.knowledge import access, kbs
from app.llm import get_chat_model

log = logging.getLogger(__name__)
PURPOSE = "fb_replay"
CONFIRMED, FAILED, INCONCLUSIVE, NOT_POSSIBLE = "confirmed", "failed", "inconclusive", "not_possible"
OUTCOMES = (CONFIRMED, FAILED, INCONCLUSIVE, NOT_POSSIBLE)
MAX_ANSWER_CHARS = 4000


def _aware(d: datetime | None) -> datetime | None:
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def rater(db: Database, fb: Feedback):
    """The rater's identity as it is NOW (role, groups, status): the replay is checked against today's access, not the access they had."""
    if fb.user_id is None:
        return None
    with db.session() as s:
        u = s.get(User, fb.user_id)
        email = u.email if u is not None else ""
    return identity.resolve(email, db) if email else None


def _trace(db: Database, fb: Feedback):
    with db.session() as s:
        t = s.get(AnswerTrace, fb.answer_id)
        if t is not None:
            s.expunge(t)
        return t


def possible(db: Database, fb: Feedback) -> tuple[bool, str]:
    """(True, "") or (False, a short code). Makes no model call."""
    who = rater(db, fb)
    if who is None or not who.known or not who.active:
        return False, "rater_unavailable"
    t = _trace(db, fb)
    if t is None or not t.complete:
        return False, "trace_missing"
    with db.session() as s:
        answer = s.get(Message, fb.answer_id)
        question = s.get(Message, t.question_id)
        if answer is None or question is None:
            return False, "message_missing"
        answered_at = _aware(t.created_at)                  # the trace is saved when the answer is finished
        kb_ids = sorted({h.get("kb_id") for h in t.hits or [] if h.get("kb_id") is not None} | ({fb.kb_id} if fb.kb_id else set()))
        if not kb_ids or any(s.get(KnowledgeBase, i) is None for i in kb_ids):
            return False, "kb_gone"
        for scope, ref in (t.prompt_versions or {}).items():
            if s.get(Prompt, ref.get("id")) is None:
                return False, "prompt_gone"
        cited = [h for h in t.hits or [] if h.get("used") and h.get("doc_id") is not None]
        docs = {d.id: d for d in s.scalars(select(Document).where(Document.id.in_({h["doc_id"] for h in cited})))} if cited else {}
    for h in cited:
        d = docs.get(h["doc_id"])
        if d is None:
            return False, "document_gone"
        if _aware(d.updated_at) and answered_at and _aware(d.updated_at) > answered_at:
            return False, "document_changed"
    for kb_id in kb_ids:
        kb = kbs.get_kb(db, kb_id)
        visible = set(access.visible_docs(db, kb, who).values()) if kb and access.can_query(db, kb, who) else set()
        if any(h["doc_id"] not in visible for h in cited if h.get("kb_id") == kb_id):
            return False, "access_lost"
    return True, ""


def conversation_context(db: Database, fb: Feedback, t: AnswerTrace) -> dict:
    """The turns before the question, as the model was given them (the most recent ones, word for word)."""
    limit = global_settings.current(db).recent_turns * 2
    with db.session() as s:
        rows = list(s.scalars(select(Message).where(Message.conversation_id == fb.conversation_id, Message.id < t.question_id)
                              .order_by(Message.id.desc()).limit(limit + 4)))
    rows.reverse()
    out: list = []
    for m in rows:
        if m.role == "assistant" and not m.complete:
            if out and out[-1][0] == "user":
                out.pop()
            continue
        out.append((m.role, m.content))
    return {"history": out[-limit:], "summary": "", "language": t.language or ""}


def _pinned(db: Database, t: AnswerTrace) -> list[tuple[str, int, str]]:
    out = []
    with db.session() as s:
        for ref in (t.prompt_versions or {}).values():
            p = s.get(Prompt, ref.get("id"))
            if p is not None:
                out.append((p.scope, p.department_id, p.text))
    return out


def settings_changed(t: AnswerTrace, rec) -> list[str]:
    """Which of the answer's inputs differ from the original's: model, retrieval, glossary, language. The prompt versions are pinned."""
    changed = []
    now_model = rec.answering_model() if rec is not None else {}
    if now_model and ((now_model.get("provider"), now_model.get("model")) != (t.provider, t.model)):
        changed.append("model")
    if rec is not None and json.dumps(rec.retrieval, sort_keys=True, default=str) != json.dumps(t.retrieval or [], sort_keys=True, default=str):
        changed.append("retrieval")
    if rec is not None and {k: [v["mode"], v["versions"]] for k, v in rec.glossary.items()} != {k: [v.get("mode"), v.get("versions")] for k, v in (t.glossary or {}).items()}:
        changed.append("glossary")
    return changed


def replay(db: Database, fb: Feedback) -> dict:
    """Ask the question again. -> {answer (masked), found, settings_changed}. Raises if the pipeline fails (the caller retries or hands over)."""
    who = rater(db, fb)
    t = _trace(db, fb)
    with db.session() as s:
        question = s.get(Message, t.question_id).content
    ctx = conversation_context(db, fb, t)
    kb_ids = sorted({h["kb_id"] for h in t.hits or [] if h.get("kb_id") is not None} | ({fb.kb_id} if fb.kb_id else set()))
    token = identity.bind(who)
    try:
        ctx_managers = [prompts.overriding(scope, dept, text) for scope, dept, text in _pinned(db, t)]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for cm in ctx_managers:
                stack.enter_context(cm)
            tokens, meta = chat_service.chat_stream(question, fb.conversation_id, kb_ids, asker="replay:" + str(fb.id), replay=ctx)
            answer = "".join(tokens)
    finally:
        identity.unbind(token)
    return {"answer": ticketing.mask(db, answer)[:MAX_ANSWER_CHARS], "found": bool(meta.get("found", True)),
            "settings_changed": settings_changed(t, ctx.get("recorder"))}


# ---- the judge ---------------------------------------------------------------------------------------------------------------------
def judge(db: Database, fb: Feedback, original: str, replayed: str, found: bool) -> tuple[str, str]:
    """(confirmed | failed | inconclusive, a short reason). One model call; anything unreadable is inconclusive."""
    comment = (fb.comment or "").strip()
    claim = f"The person's comment: {comment[:700]}" if comment else f"An automatic summary of the issue (not written by the person; treat with care): {(fb.summary or '')[:300]}"
    task = (f"Question: {_question(db, fb)[:700]}\n\nOriginal answer: {original[:1500]}\n\nAnswer given now when asked again: {replayed[:1500]}\n"
            f"The assistant found an answer in the documents now: {'yes' if found else 'no'}\n\n{claim}")
    raw = get_chat_model().invoke([
        SystemMessage(content="You check support feedback about an answer from a company knowledge-base assistant. The person marked the original answer as not "
                              "helpful. Decide whether the problem is still there in the answer given now. confirmed: the same problem is still present. failed: the "
                              "answer given now no longer has the problem. inconclusive: you cannot tell. "
                              'Reply with JSON only: {"verdict": "confirmed" | "failed" | "inconclusive", "reason": "<one short sentence, no personal data>"}. '
                              + agent_runtime.UNTRUSTED),
        HumanMessage(content="<<<DATA\n" + task + "\nDATA>>>")], purpose=PURPOSE).content
    data = agent_runtime._json(raw) or {}
    verdict = data.get("verdict") if data.get("verdict") in (CONFIRMED, FAILED, INCONCLUSIVE) else INCONCLUSIVE
    reason = ticketing.mask(db, str(data.get("reason") or ""))[:240] if data.get("verdict") in (CONFIRMED, FAILED, INCONCLUSIVE) else "The judge's answer could not be read."
    return verdict, reason


def _question(db: Database, fb: Feedback) -> str:
    t = _trace(db, fb)
    with db.session() as s:
        m = s.get(Message, t.question_id) if t is not None else None
        return m.content if m is not None else ""


def _original(db: Database, fb: Feedback) -> str:
    with db.session() as s:
        m = s.get(Message, fb.answer_id)
        return m.content if m is not None else ""


# ---- the whole check ---------------------------------------------------------------------------------------------------------------
def cached(db: Database, fb: Feedback) -> dict | None:
    """One replay per answer and rater (D53): the stored result of an earlier feedback by the same rater on the same answer.
    Never shared between raters: a replay uses one person's access."""
    with db.session() as s:
        rows = s.scalars(select(Feedback).where(Feedback.answer_id == fb.answer_id, Feedback.rater_key == fb.rater_key).order_by(Feedback.id))
        for r in rows:
            v = r.verification if isinstance(r.verification, dict) else {}
            if v.get("outcome") in OUTCOMES and v.get("replayed_at"):
                return dict(v)
    return None


def verify(db: Database, fb: Feedback, *, may_replay: bool = True) -> dict:
    """The verification record for a feedback: {outcome, reason, replay_answer, settings_changed, model_called, replayed_at}.
    `may_replay` is False when the daily cap is reached: the item then goes to a person without a model call."""
    hit = cached(db, fb)
    if hit is not None:
        return {**hit, "cached": True}
    ok, code = possible(db, fb)
    if not ok:
        return {"outcome": NOT_POSSIBLE, "reason": code, "model_called": False}
    if not may_replay:
        return {"outcome": INCONCLUSIVE, "reason": "daily_cap", "model_called": False}
    out = replay(db, fb)
    verdict, reason = judge(db, fb, _original(db, fb), out["answer"], out["found"])
    return {"outcome": verdict, "reason": reason, "replay_answer": out["answer"], "found": out["found"], "settings_changed": out["settings_changed"],
            "model_called": True, "replayed_at": datetime.now(timezone.utc).isoformat()}
