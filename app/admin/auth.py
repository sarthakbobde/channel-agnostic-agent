"""Password hashing, signed session tokens and a login rate limiter (stdlib only)."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- passwords ---------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), **_SCRYPT)
        return hmac.compare_digest(candidate, _unb64(digest))
    except (ValueError, TypeError):
        return False


_dummy: str | None = None


def dummy_verify(password: str) -> None:
    """Spend the same time as a real check, so unknown users cannot be told apart by timing."""
    global _dummy
    if _dummy is None:
        _dummy = hash_password("not-a-real-password")
    verify_password(password, _dummy)


def fingerprint(password_hash: str | None) -> str:
    """Short id of the current password; changing the password invalidates old sessions."""
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:12]


# ---- sessions ----------------------------------------------------------------
class SessionSigner:
    """Stateless signed sessions: base64(payload).hmac. Key is derived from the first master key."""

    def __init__(self, master_keys: str, ttl: int = 8 * 3600, purpose: str = "rag-admin-session-v1"):
        first = (master_keys or "").split(",")[0].strip()
        if not first:
            raise ValueError("a master key is required to sign sessions")
        self._key = hashlib.sha256(purpose.encode() + b":" + first.encode()).digest()
        self.ttl = ttl

    def _sig(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode("ascii"), hashlib.sha256).digest())

    def issue(self, email: str, pw_fp: str) -> tuple[str, str]:
        """Returns (cookie token, csrf token)."""
        csrf = secrets.token_urlsafe(24)
        payload = {"sub": email, "exp": int(time.time()) + self.ttl, "csrf": csrf, "pw": pw_fp}
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        return f"{body}.{self._sig(body)}", csrf

    def read(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(body)):
            return None
        try:
            payload = json.loads(_unb64(body))
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
            return None
        return payload


# ---- rate limiting -----------------------------------------------------------
class LoginLimiter:
    """Blocks a key after `max_failures` failures within `window` seconds."""

    def __init__(self, max_failures: int = 5, window: int = 300):
        self.max_failures, self.window = max_failures, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str) -> list[float]:
        cutoff = time.time() - self.window
        fresh = [t for t in self._fails.get(key, []) if t > cutoff]
        if fresh:
            self._fails[key] = fresh
        else:
            self._fails.pop(key, None)
        return fresh

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key)) >= self.max_failures

    def failure(self, key: str) -> None:
        with self._lock:
            self._prune(key)
            self._fails.setdefault(key, []).append(time.time())

    def success(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
