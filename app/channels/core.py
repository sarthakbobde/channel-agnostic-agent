"""The single entry point every adapter uses."""
import logging
import re
from app import catalog, chat_service, feedback, global_settings, identity, ratelimit, suggestions
from app.channels.base import Envelope

log = logging.getLogger(__name__)
SLOW_DOWN = catalog.TEXTS["slow_down"][0]
ERROR_REPLY = catalog.TEXTS["error"][0]


def _reply(key: str, env: Envelope) -> dict:
    """A reply that is not an answer from the assistant (refused, slowed down, failed): nothing to rate. It is sent in the
    language the person wrote in, when that language has approved messages."""
    lang = chat_service.reply_language(env.text, env.conv_id)
    return {"answer": catalog.t(key, lang), "answer_id": None, "found": False, "sources": [], "searched": False, "query": "",
            "language": lang, "suggestions": []}


def asker_of(env: Envelope) -> str:
    """Who is asking, as the chat app knows them: only this person may click their suggestions."""
    return f"{env.channel}:{env.workspace}:{env.user}"


def handle(env: Envelope, suggestion: dict | None = None) -> dict:
    """Run one turn. Returns {answer, answer_id, found, sources, searched, query, language, suggestions}; never raises."""
    who = identity.resolve(env.email)
    if not who.active:
        log.info("refused deactivated sender %s", who.email)
        return _reply("denied", env)
    ok, _ = ratelimit.allow("chat:" + (who.email.lower() if who.email else f"{env.channel}:{env.workspace}:{env.user}"),
                            global_settings.current().chat_rate_per_minute)
    if not ok:
        return _reply("slow_down", env)
    token = identity.bind(who)
    try:
        text = suggestion["t"] if suggestion else env.text
        result = chat_service.chat(text, env.conv_id, asker=asker_of(env), **({"kb_ids": list(env.kb_ids)} if env.kb_ids else {}))
        if suggestion:
            suggestions.link_question(suggestion.get("s"), result.get("question_id"))
        return result
    except Exception:
        log.exception("chat failed for %s", env.conv_id)
        return _reply("error", env)
    finally:
        identity.unbind(token)


def handle_rating(env: Envelope, payload: str, rating: str, reason: str = "", comment: str = "", attachments=None) -> dict:
    """A click on a rating button, or the details form that follows a thumbs-down (D49, D50). The answer text is the thanks.
    `feedback_form` asks for the reason and comment after a thumbs-down that came without them."""
    who = identity.resolve(env.email)
    if not who.active:
        return _reply("denied", env)
    try:
        data = feedback.verify(payload, suggestions.asker_key(asker_of(env)))
    except suggestions.PayloadError as e:
        return _reply(e.key, env)
    lang = chat_service.reply_language(env.text, env.conv_id)
    token = identity.bind(who)
    try:
        saved = feedback.capture(data, rating, reason, comment, attachments, who=who)
    except ValueError:
        return _reply("feedback_invalid", env)
    except Exception:
        log.exception("could not store feedback for %s", env.conv_id)
        return _reply("error", env)
    finally:
        identity.unbind(token)
    detailed = bool(reason or comment)
    out = {**_reply("feedback_updated" if detailed or not saved["created"] else "feedback_thanks", env), "feedback": saved}
    if saved["intent"] != "positive" and rating == feedback.DOWN and not detailed:
        out["feedback_form"] = {"payload": payload, "answer_id": data["a"], "reasons": feedback.reasons_offered(), "labels": feedback.labels(lang)}
    return out


def handle_action(env: Envelope, payload: str, rating: str = "", reason: str = "", comment: str = "", attachments=None) -> dict:
    """A click on a suggestion button: verify the signed payload (D37), then ask it as an ordinary question in the same
    conversation. A replay is ignored (`ignored: True`, nothing to send). A rating payload goes to handle_rating()."""
    if feedback.is_rating(payload):
        return handle_rating(env, payload, rating, reason, comment, attachments)
    try:
        data = suggestions.verify(payload, suggestions.asker_key(asker_of(env)))
    except suggestions.PayloadError as e:
        return _reply(e.key, env)
    if not suggestions.record_click(data):
        return {**_reply("error", env), "answer": "", "ignored": True}
    return handle(Envelope(env.channel, env.workspace, env.room, env.thread, env.user, data["t"], env.kb_ids, env.email), data)


def sources_footer(result: dict) -> str:
    """The italic sources line the chat apps add under an answer, in the answer's language."""
    lang = result.get("language", "")
    return f"\n\n_{catalog.t('sources', lang)}: {sources_line(result['sources'], lang)}_"


def suggestion_controls(result: dict) -> list[dict]:
    """The presenters' controls: [{type: suggestion_chip, text, payload}] (payload None = show as plain text)."""
    return [{"type": "suggestion_chip", "text": s["text"], "payload": s.get("payload")} for s in result.get("suggestions") or []]


def rating_buttons(result: dict) -> dict | None:
    """The presenters' rating control: {payload, labels, reasons} when the answer can be rated, else None."""
    ctl = result.get("feedback")
    return ctl if isinstance(ctl, dict) and ctl.get("payload") else None


def suggestions_text(result: dict) -> str:
    """Suggestions as plain text, for a chat app that cannot show buttons."""
    items = result.get("suggestions") or []
    if not items:
        return ""
    return f"\n\n_{catalog.t('suggested', result.get('language', ''))}:_\n" + "\n".join(f"• {s['text']}" for s in items)


def sources_line(sources: list[dict], lang: str = "") -> str:
    """Titles as written. A document in another language than the answer gets its language code, for example [EN]."""
    seen, out = set(), []
    for s in sources:
        label = s["source"] + (f" p.{s['page']}" if s.get("page") else "")
        if lang and s.get("doc_language") and s["doc_language"] != lang:
            label += f" [{s['doc_language'].upper()}]"
        if label not in seen:
            seen.add(label)
            out.append(label)
    return ", ".join(out)


def to_mrkdwn(text: str) -> str:
    """Convert common Markdown to Slack mrkdwn."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.M)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    return text
