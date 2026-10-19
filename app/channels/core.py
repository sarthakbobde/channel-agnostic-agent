"""The single entry point every adapter uses."""
import logging
import re
from app import chat_service, global_settings, identity, ratelimit
from app.channels.base import Envelope

log = logging.getLogger(__name__)
SLOW_DOWN = "You are asking too quickly. Please wait a moment and try again."
ERROR_REPLY = "Sorry, something went wrong while answering. Please try again."


def handle(env: Envelope) -> dict:
    """Run one turn. Returns {answer, sources, searched, query}; never raises."""
    who = identity.resolve(env.email)
    if not who.active:
        log.info("refused deactivated sender %s", who.email)
        return {"answer": identity.DENIED_REPLY, "sources": [], "searched": False, "query": ""}
    ok, _ = ratelimit.allow("chat:" + (who.email.lower() if who.email else f"{env.channel}:{env.workspace}:{env.user}"),
                            global_settings.current().chat_rate_per_minute)
    if not ok:
        return {"answer": SLOW_DOWN, "sources": [], "searched": False, "query": ""}
    token = identity.bind(who)
    try:
        return chat_service.chat(env.text, env.conv_id, **({"kb_ids": list(env.kb_ids)} if env.kb_ids else {}))
    except Exception:
        log.exception("chat failed for %s", env.conv_id)
        return {"answer": ERROR_REPLY, "sources": [], "searched": False, "query": ""}
    finally:
        identity.unbind(token)


def sources_line(sources: list[dict]) -> str:
    seen, out = set(), []
    for s in sources:
        label = s["source"] + (f" p.{s['page']}" if s.get("page") else "")
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
