"""Load channels.yaml, expanding ${ENV_VAR} references."""
import os
import re
from pathlib import Path
import yaml

_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(v):
    if isinstance(v, str):
        return _VAR.sub(lambda m: os.getenv(m.group(1), ""), v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def load(path: str | None = None) -> dict:
    p = Path(path or os.getenv("CHANNELS_CONFIG", "channels.yaml"))
    if not p.exists():
        return {"defaults": {}, "channels": {}}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _expand(raw)
    cfg.setdefault("defaults", {})
    cfg.setdefault("channels", {})
    return cfg
