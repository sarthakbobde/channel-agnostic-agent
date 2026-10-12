"""The single entry point every adapter uses."""
import logging
import re
from app import chat_service
from app.channels.base import Envelope

log = logging.getLogger(__name__)
ERROR_REPLY = "Sorry, something went wrong while answering. Please try again."


def handle(env: Envelope) -> dict:
    """Run one turn. Returns {answer, sources, searched, query}; never raises."""
    try:
        return chat_service.chat(env.text, env.conv_id)
    except Exception:
        log.exception("chat failed for %s", env.conv_id)
        return {"answer": ERROR_REPLY, "sources": [], "searched": False, "query": ""}


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
