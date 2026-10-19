"""Chat model access: a preferred model and an optional fallback, read from the admin database.

`get_chat_model()` returns a ModelChain with `invoke()` and `stream()`. If the preferred model fails
with 402 (no credit), 429 (rate limit), a 5xx error, a timeout or an unreachable provider, the same
request goes to the fallback model. A rejected request (any other 4xx) is NOT retried elsewhere:
the request itself is wrong, so another model would fail the same way.

The fallback happens before any reply is sent. Streamed output is therefore collected first; chat
apps only ever receive whole answers, so nothing the user sees changes.

Settings changes (admin page) take effect within a couple of seconds, with no restart: the models
are rebuilt from the database, never cached forever. If nothing is configured in the database the
old .env model is used, so a fresh install still answers.
"""
import logging
import threading
import time

from app import alerts, identity, usage
from app.config import settings
from app.db import Database

log = logging.getLogger(__name__)
CACHE_SECONDS = 2.0
FALLBACK_STATUS = {402, 429}


# ---- classifying failures ------------------------------------------------------------------
def _causes(exc: BaseException):
    seen = 0
    while exc is not None and seen < 5:
        yield exc
        exc = exc.__cause__ or exc.__context__
        seen += 1


def status_of(exc: BaseException) -> int | None:
    """The HTTP status behind a provider error, however the SDK exposes it."""
    for e in _causes(exc):
        for attr in ("status_code", "code", "status"):
            v = getattr(e, attr, None)
            if isinstance(v, int) and not isinstance(v, bool) and 100 <= v < 600:
                return v
        v = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(v, int) and 100 <= v < 600:
            return v
    return None


def _name_has(exc: BaseException, *needles: str) -> bool:
    return any(n in type(e).__name__.lower() for e in _causes(exc) for n in needles)


def is_timeout(exc: BaseException) -> bool:
    return any(isinstance(e, TimeoutError) for e in _causes(exc)) or _name_has(exc, "timeout")


def is_unreachable(exc: BaseException) -> bool:
    return _name_has(exc, "connecterror", "connectionerror", "apiconnection")


def should_fall_back(exc: BaseException) -> bool:
    """True for 402, 429, 5xx, timeouts and unreachable providers; False for everything else."""
    status = status_of(exc)
    if status is not None:
        return status in FALLBACK_STATUS or status >= 500
    return is_timeout(exc) or is_unreachable(exc)


def reason(exc: BaseException) -> str:
    status = status_of(exc)
    if status == 402:
        return "credit exhausted (HTTP 402)"
    if status == 429:
        return "rate limited (HTTP 429)"
    if status is not None and status >= 500:
        return f"server error (HTTP {status})"
    if status in (401, 403):
        return f"key rejected (HTTP {status})"
    if is_timeout(exc):
        return "timed out"
    if is_unreachable(exc):
        return "unreachable"
    return f"error {type(exc).__name__}" + (f" (HTTP {status})" if status else "")


def _usage(messages) -> tuple[int | None, int | None]:
    """Token counts from a response, or from streamed chunks (the largest value seen per field,
    which is right whether a provider reports cumulative or partial usage)."""
    inp = out = None
    for m in messages if isinstance(messages, list) else [messages]:
        u = getattr(m, "usage_metadata", None) or {}
        if isinstance(u.get("input_tokens"), int):
            inp = max(inp or 0, u["input_tokens"])
        if isinstance(u.get("output_tokens"), int):
            out = max(out or 0, u["output_tokens"])
    return inp, out


# ---- the model chain -----------------------------------------------------------------------------
class Slot:
    def __init__(self, provider_id: int, provider_name: str, provider_type: str, model: str, runnable):
        self.provider_id, self.provider_name, self.provider_type = provider_id, provider_name, provider_type
        self.model, self.runnable = model, runnable


class ModelChain:
    def __init__(self, slots: list[Slot], db: Database | None):
        self.slots, self.db = slots, db

    def invoke(self, messages, *, purpose: str = "answer", department: str | None = None, **kw):
        return self._run(False, messages, purpose, department, kw)

    def stream(self, messages, *, purpose: str = "answer", department: str | None = None, **kw):
        yield from self._run(True, messages, purpose, department, kw)

    def _call(self, slot: Slot, streaming: bool, messages, kw):
        if streaming:
            chunks = list(slot.runnable.stream(messages, **kw))        # collected: a fallback may still be needed
            return chunks, _usage(chunks)
        message = slot.runnable.invoke(messages, **kw)
        return message, _usage(message)

    def _run(self, streaming, messages, purpose, department, kw):
        last = None
        department = department or identity.current_department()
        for index, slot in enumerate(self.slots):
            try:
                result, (tokens_in, tokens_out) = self._call(slot, streaming, messages, kw)
            except Exception as exc:
                last = exc
                self._failed(slot, exc, purpose, department)
                if not should_fall_back(exc):
                    raise                                      # a rejected request: do not try another model
                continue
            self._succeeded(slot, index, purpose, department, tokens_in, tokens_out)
            return result
        self._alert("llm_unavailable", "llm_unavailable", "error",
                    "No model could answer: " + "; ".join(
                        f"{s.provider_name}/{s.model}" for s in self.slots) + f" all failed. Last problem: {reason(last)}.")
        raise last

    # recording and alerting never raise: a bookkeeping problem must not break an answer
    def _alert(self, key: str, kind: str, severity: str, message: str) -> None:
        if self.db is not None:
            try:
                alerts.raise_alert(self.db, key, kind, message, severity)
            except Exception:
                log.warning("could not raise alert %s", key, exc_info=True)

    def _failed(self, slot: Slot, exc: Exception, purpose: str, department: str | None) -> None:
        status = status_of(exc)
        label = f"{type(exc).__name__}" + (f" HTTP {status}" if status else "")
        log.warning("model %s/%s failed: %s", slot.provider_name, slot.model, reason(exc))
        if self.db is not None:
            usage.record(self.db, provider=slot.provider_name, provider_type=slot.provider_type, model=slot.model,
                         purpose=purpose, department=department, input_tokens=None, output_tokens=None,
                         status="failed", error=label)
        if status in (401, 402, 403):
            self._alert(f"provider_failing:{slot.provider_name}", "provider_failing", "error",
                        f"{slot.provider_name}: {reason(exc)}. "
                        + ("Add credit or use another provider." if status == 402 else "Check the API key."))

    def _succeeded(self, slot: Slot, index: int, purpose: str, department: str | None,
                   tokens_in, tokens_out) -> None:
        if self.db is None:
            return
        usage.record(self.db, provider=slot.provider_name, provider_type=slot.provider_type, model=slot.model,
                     purpose=purpose, department=department, input_tokens=tokens_in, output_tokens=tokens_out,
                     status="ok")
        try:
            alerts.resolve_key(self.db, f"provider_failing:{slot.provider_name}")
            alerts.resolve_key(self.db, "llm_unavailable")
        except Exception:
            log.warning("could not resolve alerts", exc_info=True)
        if index > 0:
            first = self.slots[0]
            self._alert("fallback_used", "fallback_used", "warning",
                        f"{first.provider_name}/{first.model} failed, so answers are coming from the fallback "
                        f"{slot.provider_name}/{slot.model}. Check the preferred provider.")
        else:
            try:
                alerts.resolve_key(self.db, "fallback_used")
            except Exception:
                pass


# ---- building the chain from the database ----------------------------------------------------------
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "key": None, "chain": None}


def _dependencies():
    """(database, secret store); a seam for tests."""
    from app.db import get_db
    from app.secretstore import default_store
    return get_db(), default_store()


def invalidate() -> None:
    """Rebuild the models on the next call (called after any change in the admin page)."""
    with _lock:
        _cache.update(ts=0.0, key=None, chain=None)


def _build_from_database() -> ModelChain | None:
    from app.providers import adapter_class
    from app.providers.store import ProviderStore
    db, secrets = _dependencies()
    if secrets is None:
        return None
    store = ProviderStore(db, secrets)
    ms = store.model_settings()
    if not ms or not ms["preferred"]["provider_id"]:
        return None
    slots = []
    for choice in (ms["preferred"], ms["fallback"]):
        if not choice:
            continue
        found = store.runtime(choice["provider_id"])
        if found is None:
            if not slots:                                  # preferred provider was deleted
                return None
            continue
        info, cfg = found
        runnable = adapter_class(info["type"])(cfg).build(choice["model"], ms["request_timeout"])
        slots.append(Slot(info["id"], info["name"], info["type"], choice["model"], runnable))
    return ModelChain(slots, db)


def _legacy_chain() -> ModelChain:
    """The .env model, used until providers are configured (a fresh install)."""
    if not settings.api_key or not settings.llm_model:
        raise RuntimeError("No model is configured. Add a provider on the admin page's Providers screen.")
    from langchain_openai import ChatOpenAI
    runnable = ChatOpenAI(base_url=settings.base_url, api_key=settings.api_key, model=settings.llm_model,
                          temperature=0, max_retries=0, timeout=60)
    try:
        db, _ = _dependencies()
    except Exception:
        db = None
    return ModelChain([Slot(0, "env", "openai_compatible", settings.llm_model, runnable)], db)


def get_chat_model() -> ModelChain:
    with _lock:
        if time.monotonic() - _cache["ts"] < CACHE_SECONDS and _cache["chain"] is not None:
            return _cache["chain"]
    chain = None
    try:
        chain = _build_from_database()
    except Exception:
        log.exception("could not build the configured models")
        raise
    if chain is None:
        chain = _legacy_chain()
    with _lock:
        _cache.update(ts=time.monotonic(), chain=chain)
    return chain
