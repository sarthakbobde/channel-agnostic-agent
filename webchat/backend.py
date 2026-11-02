"""The web chat's link to the agent's API. Chainlit stores nothing of its own: sign-in, answers and history all go
through the API with each person's own session token, so the API's rules (who may ask, whose history is whose) apply
exactly as they do for every other channel."""
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from chainlit.data.base import BaseDataLayer
from chainlit.types import Feedback, PageInfo, PaginatedResponse, Pagination, ThreadDict, ThreadFilter
from chainlit.user import PersistedUser, User

log = logging.getLogger("webchat")

BACKEND_URL = os.getenv("RAG_BACKEND_URL", "http://localhost:8000").rstrip("/")
ASK_TIMEOUT = 120                     # a cold start, retrieval and the model's own 60-second limit
transport: httpx.AsyncBaseTransport | None = None        # tests put a fake API here

# Chainlit asks the data layer about a thread by its id alone, so the session token of each signed-in person and the
# owner of each conversation they have listed are remembered here. Both come back after a restart: the token from the
# signed sign-in cookie (create_user), the owners when the history list loads. That is why the service runs one instance.
_tokens: dict[str, str] = {}
_owners: dict[str, str] = {}


def _client(timeout: float = 10) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BACKEND_URL, timeout=timeout, transport=transport)


def _detail(r: httpx.Response, default: str) -> str:
    try:
        return r.json().get("detail") or default
    except ValueError:
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- calls the pages make ----------
async def sign_in(email: str, password: str) -> User | None:
    try:
        async with _client() as c:
            r = await c.post("/chat-auth/login", json={"email": email, "password": password})
    except httpx.HTTPError as e:
        log.warning("sign-in could not reach the API: %s", e)
        return None
    if r.status_code != 200:
        return None
    d = r.json()
    _tokens[d["email"]] = d["token"]
    return User(identifier=d["email"], display_name=d.get("name") or d["email"],
                metadata={"token": d["token"], "role": d.get("role", "")})


async def set_password(token: str, password: str) -> tuple[bool, str]:
    try:
        async with _client() as c:
            r = await c.post("/chat-auth/set-password", json={"token": token, "password": password})
    except httpx.HTTPError:
        return False, "Cannot reach the service. Try again in a minute."
    if r.status_code == 200:
        return True, "Password saved."
    return False, _detail(r, "That did not work.")


async def ask(token: str, conv_id: str, message: str) -> tuple[int, dict]:
    """(status, body). Status 0 means the API could not be reached in time."""
    try:
        async with _client(ASK_TIMEOUT) as c:
            r = await c.post("/chat", json={"message": message, "conv_id": conv_id},
                             headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException:
        return 0, {"detail": "The answer took too long. Please try again."}
    except httpx.HTTPError:
        return 0, {"detail": "Cannot reach the service. Try again in a minute."}
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


async def act(token: str, conv_id: str, payload: str) -> tuple[int, dict]:
    """A click on a suggestion button: the API checks the signed payload and asks it in the same conversation."""
    try:
        async with _client(ASK_TIMEOUT) as c:
            r = await c.post("/chat/action", json={"conv_id": conv_id, "payload": payload}, headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException:
        return 0, {"detail": "The answer took too long. Please try again."}
    except httpx.HTTPError:
        return 0, {"detail": "Cannot reach the service. Try again in a minute."}
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, body if isinstance(body, dict) else {}


def suggestions_text(items: list[dict], label: str = "Suggested questions") -> str:
    """Suggestions that cannot be buttons (expired, or an older answer) as plain text."""
    return f"\n\n**{label}**\n" + "\n".join(f"- {s['text']}" for s in items) if items else ""


_resume: dict[str, list[dict]] = {}          # thread id -> the last answer's suggestions, shown again as buttons on reopen


def resume_suggestions(thread_id: str) -> list[dict]:
    return _resume.pop(thread_id, [])


def token_for(user: User | None) -> str:
    if user is None:
        return ""
    return _tokens.get(user.identifier) or (user.metadata or {}).get("token", "")


def step_id(message_id, fallback: str) -> str:
    """The id Chainlit shows a message under. A saved answer keeps its API message id, so a rating can name it."""
    return f"msg-{message_id}" if message_id else fallback


# The Web UI's own texts in English; the API's message catalogue gives them in other languages.
TEXTS = {"sources": "Sources", "suggested": "Suggested questions", "web_session_ended": "Your session has ended. Sign out (top right) and sign in again.",
         "web_try_again": "Please try again.", "web_error": "Something went wrong on our side (error {status}). Please try again."}
TEXTS_SECONDS = 300
_texts: dict[str, tuple[float, dict]] = {}


async def texts(token: str, lang: str) -> dict:
    """The Web UI texts in `lang` (cached for a few minutes). English, or the last copy, when the API cannot say."""
    if not lang or lang == "en":
        return TEXTS
    hit = _texts.get(lang)
    if hit and time.monotonic() - hit[0] < TEXTS_SECONDS:
        return hit[1]
    try:
        async with _client() as c:
            r = await c.get(f"/catalog/{lang}", headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            got = {k: v for k, v in r.json().items() if k in TEXTS and isinstance(v, str)}
            _texts[lang] = (time.monotonic(), {**TEXTS, **got})
            return _texts[lang][1]
    except (httpx.HTTPError, ValueError):
        log.warning("could not fetch the %s texts", lang)
    return hit[1] if hit else TEXTS


def with_sources(answer: str, sources: list[dict], lang: str = "", label: str = "Sources") -> str:
    """Titles as written; a document in another language than the answer gets its language code, for example (EN)."""
    if not sources:
        return answer
    lines = [f"- [{s.get('n', i + 1)}] {s.get('source', '')}" + (f", p.{s['page']}" if s.get("page") else "")
             + (f" ({s['doc_language'].upper()})" if lang and s.get("doc_language") and s["doc_language"] != lang else "")
             for i, s in enumerate(sources)]
    return answer + f"\n\n**{label}**\n" + "\n".join(lines)


# ---------- history, for Chainlit's sidebar ----------
class BackendDataLayer(BaseDataLayer):
    """Threads are the API's conversations (the thread id is the conversation id). Messages are saved by /chat, so
    Chainlit's own step and element writes are ignored."""

    def __init__(self, assistant_name: str = "Assistant"):
        self.assistant_name = assistant_name

    @staticmethod
    def _user(identifier: str) -> PersistedUser:
        # No token in here: Chainlit hands this user to the page's scripts (/user). The token stays in the httponly
        # cookie and in _tokens.
        return PersistedUser(id=identifier, identifier=identifier, createdAt=_now(), metadata={})

    async def get_user(self, identifier: str) -> PersistedUser | None:
        return self._user(identifier) if identifier in _tokens else None

    async def create_user(self, user: User) -> PersistedUser | None:
        token = (user.metadata or {}).get("token")
        if not token:
            return None
        _tokens[user.identifier] = token
        return self._user(user.identifier)

    async def _get(self, owner: str, path: str) -> httpx.Response | None:
        token = _tokens.get(owner)
        if not token:
            return None
        try:
            async with _client() as c:
                return await c.get(path, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            log.warning("history request failed: %s", e)
            return None

    async def list_threads(self, pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]:
        owner = filters.userId or ""
        r = await self._get(owner, "/conversations")
        convs = r.json() if r is not None and r.status_code == 200 else []
        if filters.search:
            convs = [c for c in convs if filters.search.lower() in (c.get("title") or "").lower()]
        for c in convs:
            _owners[c["id"]] = owner
        if pagination.cursor:
            ids = [c["id"] for c in convs]
            convs = convs[ids.index(pagination.cursor) + 1:] if pagination.cursor in ids else []
        page = convs[:pagination.first]
        threads = [ThreadDict(id=c["id"], createdAt=c.get("updated_at") or _now(), name=c.get("title") or "New chat",
                              userId=owner, userIdentifier=owner, tags=None, metadata={}, steps=[], elements=[])
                   for c in page]
        return PaginatedResponse(data=threads, pageInfo=PageInfo(
            hasNextPage=len(convs) > len(page), startCursor=page[0]["id"] if page else None,
            endCursor=page[-1]["id"] if page else None))

    async def get_thread_author(self, thread_id: str) -> str:
        return _owners.get(thread_id, "")

    async def get_thread(self, thread_id: str) -> ThreadDict | None:
        owner = _owners.get(thread_id)
        if not owner:
            return None
        r = await self._get(owner, f"/conversations/{thread_id}")
        if r is None or r.status_code != 200:
            return None
        messages = r.json().get("messages", [])
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        live = [s for s in (messages[last].get("suggestions") or []) if s.get("payload")] if last >= 0 else []
        if live:
            _resume[thread_id] = live
        start = datetime.now(timezone.utc) - timedelta(seconds=len(messages))
        steps = []
        for i, m in enumerate(messages):
            mine = m.get("role") == "user"
            ts = (start + timedelta(seconds=i)).isoformat()
            steps.append({"id": step_id(m.get("id"), f"{thread_id}:{i}"), "threadId": thread_id, "parentId": None,
                          "name": owner if mine else self.assistant_name,
                          "type": "user_message" if mine else "assistant_message",
                          "output": m.get("content", "") if mine else
                          with_sources(m.get("content", ""), (m.get("meta") or {}).get("sources", []), (m.get("meta") or {}).get("language", ""))
                          + suggestions_text([s for s in m.get("suggestions") or [] if i != last or not s.get("payload")]),
                          "input": "", "createdAt": ts, "start": ts, "end": ts, "streaming": False,
                          "isError": False, "waitForAnswer": False, "metadata": {}, "showInput": False})
        first = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
        return ThreadDict(id=thread_id, createdAt=steps[0]["createdAt"] if steps else _now(),
                          name=first[:50] or "New chat", userId=owner, userIdentifier=owner, tags=None,
                          metadata={}, steps=steps, elements=[])

    async def delete_thread(self, thread_id: str):
        owner = _owners.get(thread_id)
        token = _tokens.get(owner or "")
        if not token:
            return
        async with _client() as c:
            await c.delete(f"/conversations/{thread_id}", headers={"Authorization": f"Bearer {token}"})
        _owners.pop(thread_id, None)

    async def update_thread(self, thread_id: str, name=None, user_id=None, metadata=None, tags=None):
        if user_id:                                         # the first message of a new chat
            _owners[thread_id] = user_id

    async def upsert_feedback(self, feedback: Feedback) -> str:
        # Kept in the service log for the UAT review until the API stores ratings. The comment itself is never logged:
        # it is free text that may hold personal data.
        log.info("feedback %s on thread %s, message %s, %s", feedback.value, feedback.threadId, feedback.forId,
                 "with a comment" if feedback.comment else "no comment")
        return feedback.id or str(uuid.uuid4())

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    async def create_step(self, step_dict):
        pass

    async def update_step(self, step_dict):
        pass

    async def delete_step(self, step_id: str):
        pass

    async def create_element(self, element):
        pass

    async def get_element(self, thread_id: str, element_id: str):
        return None

    async def delete_element(self, element_id: str, thread_id: str | None = None):
        pass

    async def get_favorite_steps(self, user_id: str):
        return []

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass
