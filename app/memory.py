import json
import re
import time
from pathlib import Path
from app import global_settings
from app.config import settings


def _dir() -> Path:
    p = Path(settings.chat_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(conv_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", conv_id)   # block path tricks
    return _dir() / f"{safe}.txt"


def _read(conv_id: str) -> list[dict]:
    p = _path(conv_id)
    if not p.exists():
        return []
    msgs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue                      # skip a corrupt line
    return msgs


def add(conv_id: str, role: str, content: str, meta: dict | None = None) -> None:
    """Append one message. role is 'user' or 'assistant'."""
    rec = {"role": role, "content": content, "meta": meta or {}, "ts": time.time()}
    with _path(conv_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def get(conv_id: str) -> list[tuple[str, str]]:
    """Last N turns as (role, content), oldest first. Used as LLM context."""
    msgs = _read(conv_id)[-global_settings.current().recent_turns * 2:]
    return [(m["role"], m["content"]) for m in msgs]


def get_messages(conv_id: str) -> list[dict]:
    """Full history for display."""
    return [
        {"role": m["role"], "content": m["content"], "meta": m.get("meta", {})}
        for m in _read(conv_id)
    ]


def list_conversations(prefix: str = "") -> list[dict]:
    """Newest first. Title = first user message. `prefix` limits to one person's conversations."""
    try:
        files = sorted(_dir().glob("*.txt"), key=lambda f: f.stat().st_mtime, reverse=True)
    except Exception as e:
        print(f"Error listing conversation files: {e}")
        return []
    
    out = []
    for f in files:
        if prefix and not f.stem.startswith(prefix):
            continue
        try:
            title = "New chat"
            for m in _read(f.stem):
                if m["role"] == "user":
                    title = m["content"].replace("\n", " ")[:50]
                    break
            out.append({"id": f.stem, "title": title})
        except Exception as e:
            print(f"Error reading conversation {f.stem}: {e}")
            continue
    return out


def delete(conv_id: str) -> None:
    _path(conv_id).unlink(missing_ok=True)


clear = delete