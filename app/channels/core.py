"""The single entry point every adapter uses."""
import logging
import re
from app import catalog, chat_service, global_settings, identity, ratelimit
from app.channels.base import Envelope

log = logging.getLogger(__name__)
SLOW_DOWN = catalog.TEXTS["slow_down"][0]
ERROR_REPLY = catalog.TEXTS["error"][0]


def _reply(key: str, env: Envelope) -> dict:
    """A reply that is not an answer from the assistant (refused, slowed down, failed): nothing to rate. It is sent in the
    language the person wrote in, when that language has approved messages."""
    lang = chat_service.reply_language(env.text, env.conv_id)
    return {"answer": catalog.t(key, lang), "answer_id": None, "found": False, "sources": [], "searched": False, "query": "",
            "language": lang}


def handle(env: Envelope) -> dict:
    """Run one turn. Returns {answer, answer_id, found, sources, searched, query, language}; never raises."""
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
        return chat_service.chat(env.text, env.conv_id, **({"kb_ids": list(env.kb_ids)} if env.kb_ids else {}))
    except Exception:
        log.exception("chat failed for %s", env.conv_id)
        return _reply("error", env)
    finally:
        identity.unbind(token)


def sources_footer(result: dict) -> str:
    """The italic sources line the chat apps add under an answer, in the answer's language."""
    lang = result.get("language", "")
    return f"\n\n_{catalog.t('sources', lang)}: {sources_line(result['sources'], lang)}_"


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
