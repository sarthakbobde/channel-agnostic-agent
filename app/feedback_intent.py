"""The intent agent (Release 3, Phase 3; D52): is this feedback a real complaint, and what is the issue in a sentence?

* With a comment: the model reads the masked comment (with the question and answer) and says positive or negative. A comment that is
  unclear or mixed counts as negative, because a replay or a person checks it next.
* A plain thumbs-down: negative by rule. The model reads the trace through read-only tools, under the RATER's allow-list, and writes the
  issue summary. The tools are bound to this one feedback in code; the model cannot name another.

The agent never changes a state: it returns `{intent, source, summary}` and the orchestrator applies it. Output is checked in code
(the intent is one of two words; the summary is masked and cut), and what the agent reads is untrusted data.
"""
import logging

from app import agent_runtime, global_settings, ticketing
from app.agent_runtime import Limits, Tool
from app.db import AnswerTrace, Database, Document, Feedback, Message, get_db
from app.knowledge import access, kbs

log = logging.getLogger(__name__)
PURPOSE = "fb_intent"
POSITIVE, NEGATIVE = "positive", "negative"
MAX_SUMMARY = 300

SYSTEM = ("You review feedback that a person gave on an answer from a company knowledge-base assistant, and decide whether it is a real complaint about "
          "the answer. positive: the person is happy, or says nothing against the answer. negative: the person reports a problem (a wrong, missing, "
          "outdated, confusing or unhelpful answer, or an answer they were not allowed to see). If the comment is unclear or mixed, choose negative. "
          "Also write the issue in one short sentence (at most 30 words, no names, no personal data); for positive feedback the issue is empty.")


def limits(db: Database) -> Limits:
    g = global_settings.current(db)
    return Limits(g.feedback_agent_max_steps, g.feedback_agent_timeout_seconds, g.feedback_agent_token_ceiling)


def tools_for(db: Database, fb: Feedback, who) -> dict[str, Tool]:
    """Read-only tools bound to ONE feedback and to the rater's access. The model passes no ids, so it cannot ask about anything else."""
    def trace() -> AnswerTrace | None:
        with db.session() as s:
            t = s.get(AnswerTrace, fb.answer_id)
            if t is not None:
                s.expunge(t)
            return t

    def visible_ids(kb_id) -> set:
        kb = kbs.get_kb(db, kb_id) if kb_id else None
        return set(access.visible_docs(db, kb, who).values()) if kb and who is not None and access.can_query(db, kb, who) else set()

    def titles(ids) -> dict:
        with db.session() as s:
            return {d.id: d.name for d in s.query(Document).filter(Document.id.in_(list(ids))).all()} if ids else {}

    def get_trace(_args) -> dict:
        t = trace()
        if t is None:
            return {"error": "No trace for this answer."}
        used = [h for h in t.hits or [] if h.get("used")]
        scores = [h.get("score") for h in t.hits or [] if h.get("score") is not None]
        return {"answer_found_in_documents": bool(t.found), "searched": bool(t.searched), "search_text": (t.standalone_query or "")[:200],
                "passages_found": len(t.hits or []), "passages_used": len(used), "best_score": round(max(scores), 3) if scores else None}

    def get_cited(_args) -> list:
        t = trace()
        ids = {h["doc_id"] for h in (t.hits if t else []) or [] if h.get("used") and h.get("doc_id") is not None}
        ok = {i for i in ids if i in visible_ids(next((h.get("kb_id") for h in t.hits if h.get("doc_id") == i), None))}
        names = titles(ok)
        return [{"doc_id": i, "title": names.get(i, "")} for i in sorted(ok)] + ([{"restricted_documents": len(ids - ok)}] if ids - ok else [])

    def get_retrieval(_args) -> list:
        t = trace()
        out = []
        for h in (t.hits if t else []) or []:
            if h.get("doc_id") in visible_ids(h.get("kb_id")):
                out.append({"doc_id": h["doc_id"], "rank": h.get("rank"), "score": h.get("score"), "above_threshold": h.get("above_threshold"), "used": h.get("used")})
        return out[:10]

    return {"get_trace": Tool("get_trace", "How the answer was found: whether documents answered it, how many passages were found and used, the best score.", get_trace),
            "get_cited_documents": Tool("get_cited_documents", "The documents the answer cited that this person may see (restricted ones are only counted).", get_cited),
            "get_retrieval": Tool("get_retrieval", "The top retrieved passages as document id, rank, score, above-threshold and used. No passage text.", get_retrieval)}


def _validate(comment: bool, db: Database):
    def check(final: dict) -> dict:
        intent = str(final.get("intent", "")).strip().lower()
        if intent not in (POSITIVE, NEGATIVE):
            raise ValueError("intent")
        summary = ticketing.mask(db, str(final.get("summary") or ""))[:MAX_SUMMARY].strip() if intent == NEGATIVE else ""
        return {"intent": intent, "summary": summary}
    return check


def classify(db: Database, fb: Feedback, who=None) -> agent_runtime.RunResult:
    """Run the agent. On success `result.output` = {intent, source, summary}; a thumbs-down with no comment is always negative (D52)."""
    commented = bool((fb.comment or "").strip())
    with db.session() as s:
        answer = s.get(Message, fb.answer_id)
        t = s.get(AnswerTrace, fb.answer_id)
        question = s.get(Message, t.question_id) if t is not None else None
        text = f"Rating: {'not helpful' if fb.rating == 'down' else 'helpful'}\nComment: {fb.comment or '(none)'}\n"
        text += f"Question: {(question.content if question else '')[:700]}\nAnswer: {(answer.content if answer else '')[:1200]}"
    thumbs_only = fb.rating == "down" and not commented
    res = agent_runtime.run(
        "intent", system=SYSTEM + (" This person gave a thumbs-down with no comment: it is negative. Use the tools to see how the answer was found, then write the issue." if thumbs_only else ""),
        task=text, answer_format='{"final": {"intent": "positive" | "negative", "summary": "<the issue>"}}', validate=_validate(commented, db),
        tools=tools_for(db, fb, who) if thumbs_only else {}, limits=limits(db), purpose=PURPOSE,
        enabled=lambda: global_settings.current(db).feedback_intent_enabled)
    if res.ok:
        if thumbs_only:
            res.output["intent"] = NEGATIVE                       # by rule, whatever the model said
            res.output["summary"] = res.output["summary"] or "The person marked the answer not helpful and left no comment."
        res.output["source"] = "comment" if commented else "trace"
    return res
