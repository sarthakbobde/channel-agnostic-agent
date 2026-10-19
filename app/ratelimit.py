"""A small sliding-window limiter for chat questions (in memory; the server runs as one process).

`allow(key, limit)` is True when this person has asked fewer than `limit` questions in the last minute (and records this
one). A limit of 0 switches the limiter off. The limit is a Global Setting, so an Admin can change it without a restart.
"""
import threading
import time
from collections import defaultdict, deque

WINDOW = 60.0
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()
_last_sweep = 0.0


def allow(key: str, limit: int, now: float | None = None) -> tuple[bool, int]:
    """-> (allowed, seconds to wait when refused)."""
    global _last_sweep
    if limit <= 0:
        return True, 0
    now = time.monotonic() if now is None else now
    with _lock:
        if now - _last_sweep > WINDOW:                     # forget people who have gone quiet, so the table cannot grow for ever
            _last_sweep = now
            for k in [k for k, q in _hits.items() if not q or now - q[-1] > WINDOW]:
                del _hits[k]
        q = _hits[key]
        while q and now - q[0] >= WINDOW:
            q.popleft()
        if len(q) >= limit:
            return False, max(1, int(WINDOW - (now - q[0])) + 1)
        q.append(now)
        return True, 0


def reset() -> None:
    global _last_sweep
    with _lock:
        _hits.clear()
        _last_sweep = 0.0
