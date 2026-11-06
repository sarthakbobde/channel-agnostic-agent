"""FastAPI REST API for Channel Agnostic Agent."""
import logging
import threading
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import catalog, feedback, suggestions, channels, chat_auth, chat_service, global_settings, guardrails, identity, language, memory, prompts, ratelimit, retention, summaries
from app.knowledge import storehealth
from app.channels import health as channel_health
from app.jobs import scheduler
from app.providers import seed as provider_seed
from app.knowledge import doc_summary as knowledge_doc_summary, documents as knowledge_documents, kbs as knowledge_kbs
from app.admin.api import check_local
from app.admin.server import include_admin, listener as admin_listener
from app.config import settings
from app.security import Caller, require_chat_caller

log = logging.getLogger(__name__)


class MessageRequest(BaseModel):
    message: str
    conv_id: str


class ChatResponse(BaseModel):
    answer: str
    answer_id: int | None = None      # the saved answer (for ratings); None when nothing was answered
    found: bool = False               # False for "could not find this", a refusal or an error
    sources: list[dict]
    searched: bool
    query: str = ""
    language: str = ""                # the language the answer is written in (D19)
    rejected: str | None = None       # "language" when the question's language is not accepted (D39)
    suggestions: list[dict] = []      # [{text, suggestion_id, payload}]; payload None = show as plain text (D26, D37)
    feedback: dict | None = None      # {payload, answer_id, labels} when the answer can be rated (Release 3)


class ActionRequest(BaseModel):
    conv_id: str
    payload: str


class FeedbackRequest(BaseModel):
    conv_id: str
    payload: str
    rating: str
    comment: str = ""
    attachments: list[str] = []


class ConversationSummary(BaseModel):
    id: str
    title: str


class ConversationHistory(BaseModel):
    id: str
    messages: list[dict]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    log.info("Channel Agnostic Agent started")
    channels.start_all()
    provider_seed.run()
    knowledge_kbs.run_startup()
    try:
        prompts.seed()
    except Exception:
        log.exception("could not seed the prompts; the built-in text is used")
    try:
        catalog.seed()
    except Exception:
        log.exception("could not seed the message catalogue; the English texts are used")
    try:
        imported = memory.import_files()
        if imported["imported"]:
            log.info("imported %d conversations from the old text files (kept as a backup)", imported["imported"])
    except Exception:
        log.exception("could not import the old conversation files")
    try:
        guardrails.seed()
        threading.Thread(target=guardrails.warm_up, name="guardrail-warm-up", daemon=True).start()
    except Exception:
        log.exception("guardrail set-up failed; questions will be refused until it is fixed")
    scheduler.start()
    try:
        retention.register()
        storehealth.register()
        channel_health.register()
    except Exception:
        log.exception("could not schedule the retention job")
    if settings.admin_port:
        admin_listener.start()
    yield
    admin_listener.stop()
    knowledge_documents.shutdown()
    knowledge_doc_summary.shutdown()
    summaries.shutdown()
    scheduler.stop()
    channels.stop_all()
    log.info("Channel Agnostic Agent shutdown")


app = FastAPI(
    title="Channel Agnostic Agent",
    description="Retrieval-Augmented Generation chatbot engine",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,      # built-in docs are public; replaced by local-only routes below
)

# CORS: no browser origin is trusted unless listed in CORS_ORIGINS. (The web chat server and the chat
# platforms call this server directly, not from a browser page, so they are unaffected.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


channels.mount(app)
if not settings.admin_port:                    # with ADMIN_PORT set, the admin runs on its own listener instead
    include_admin(app)
app.include_router(chat_auth.router)


@app.get("/health")
def health():
    """Health check endpoint. `guardrails`: off | ready | loading | error (the detection model is loaded once at start-up).
    `language`: ready | missing (without the detector every question is answered in the default language)."""
    return {"status": "ok", "guardrails": guardrails.status(), "language": language.status()}


# API documentation: reachable only from this machine, never through a tunnel or proxy.
@app.get("/openapi.json", include_in_schema=False)
def openapi_schema(request: Request):
    check_local(request, allow_remote=False)
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def swagger_ui(request: Request):
    check_local(request, allow_remote=False)
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} API")


def _audit(actor: str, action: str, target: str, detail: str = "") -> None:
    try:
        store = channels.get_store()
        if store:
            store.record(actor, action, target, detail)
    except Exception:
        log.warning("could not audit %s", action, exc_info=True)


PROVIDER_REFUSED_REPLY = catalog.TEXTS["provider_refused"][0]


def _refused_by_provider(e: BaseException) -> bool:
    """True when the model host (for example Azure OpenAI) rejected the prompt with its own content filter."""
    text = str(e).lower()
    return "content_filter" in text or "content management policy" in text


def _conv(caller: Caller, conv_id: str) -> str:
    """A person's conversations live under their own prefix, so nobody can read or delete another's."""
    return caller.prefix + conv_id


def _asker(caller: Caller) -> str:
    return "web:" + (caller.email.lower() or caller.prefix or "service")


@app.post("/chat", response_model=ChatResponse)
def chat(request: MessageRequest, caller: Caller = Depends(require_chat_caller)):
    """Chat endpoint - returns answer with sources."""
    return _answer(request, caller)


def _answer(request: MessageRequest, caller: Caller, suggestion: dict | None = None) -> ChatResponse:
    ok, wait = ratelimit.allow("chat:" + (caller.email.lower() or "service"), global_settings.current().chat_rate_per_minute)
    if not ok:
        lang = chat_service.reply_language(request.message, _conv(caller, request.conv_id))
        raise HTTPException(429, catalog.t("slow_down_wait", lang, seconds=wait), headers={"Retry-After": str(wait)})
    who = identity.resolve(caller.email)
    token = identity.bind(who)
    try:
        kb_ids = chat_auth.web_kb_ids()
        answer_iter, meta = chat_service.chat_stream(request.message, _conv(caller, request.conv_id), asker=_asker(caller),
                                                     **({"kb_ids": kb_ids} if kb_ids else {}))
        answer = "".join(answer_iter)
        if suggestion:
            suggestions.link_question(suggestion.get("s"), meta.get("question_id"))

        return ChatResponse(
            answer=answer,
            answer_id=meta.get("answer_id"),
            found=meta.get("found", False),
            sources=meta.get("sources", []),
            searched=meta.get("searched", False),
            query=meta.get("query", ""),
            language=meta.get("language", ""),
            rejected=meta.get("rejected"),
            suggestions=meta.get("suggestions") or [],
            feedback=meta.get("feedback"),
        )
    except Exception as e:
        if _refused_by_provider(e):
            log.warning("the AI provider's own content filter refused the request: %s", e)
            lang = chat_service.reply_language(request.message, _conv(caller, request.conv_id))
            return ChatResponse(answer=catalog.t("provider_refused", lang), sources=[], searched=False, query="", language=lang)
        log.error(f"Chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")
    finally:
        identity.unbind(token)


@app.post("/chat/action", response_model=ChatResponse)
def chat_action(request: ActionRequest, caller: Caller = Depends(require_chat_caller)):
    """A click on a suggestion: the signed payload is checked (only the asker, not expired, once), then it is asked as an
    ordinary question in the same conversation."""
    try:
        data = suggestions.verify(request.payload, suggestions.asker_key(_asker(caller)))
    except suggestions.PayloadError as e:
        lang = memory.last_language(_conv(caller, request.conv_id))
        raise HTTPException(403 if e.key == "suggestion_not_yours" else 410 if e.key == "suggestion_expired" else 400,
                            catalog.t(e.key, lang))
    if not suggestions.record_click(data):
        raise HTTPException(409, "Already asked.")
    return _answer(MessageRequest(message=data["t"], conv_id=request.conv_id), caller, data)


@app.post("/chat/feedback")
def chat_feedback(request: FeedbackRequest, caller: Caller = Depends(require_chat_caller)):
    """A rating of an answer (D49, D50): the signed payload is checked (only the asker, not expired), then it is stored.
    -> {message, state, form} where `form` asks for a comment and a link after a thumbs-down that came without them."""
    conv = _conv(caller, request.conv_id)
    lang = memory.last_language(conv)
    asker = suggestions.asker_key(_asker(caller))
    try:
        data = feedback.verify(request.payload, asker)
    except suggestions.PayloadError as e:
        raise HTTPException(403 if e.key == "feedback_not_yours" else 410 if e.key == "feedback_expired" else 400, catalog.t(e.key, lang))
    who = identity.resolve(caller.email)
    if not who.active:
        raise HTTPException(403, catalog.t("denied", lang))
    token = identity.bind(who)
    try:
        saved = feedback.capture(data, request.rating, request.comment, request.attachments, who=who)
    except ValueError:
        raise HTTPException(400, catalog.t("feedback_invalid", lang))
    except Exception:
        log.error("could not store feedback", exc_info=True)
        raise HTTPException(500, "Internal error")
    finally:
        identity.unbind(token)
    detailed = bool(request.comment or request.attachments)
    form = None
    if saved["intent"] != "positive" and request.rating == feedback.DOWN and not detailed:
        form = feedback.form_for(request.payload, data["a"], lang)
    return {"message": catalog.t("feedback_updated" if detailed or not saved["created"] else "feedback_thanks", lang),
            "state": saved["state"], "form": form}


@app.get("/catalog/{lang}")
def message_texts(lang: str, caller: Caller = Depends(require_chat_caller)):
    """The system texts in one language, with the fallbacks applied (the Web UI shows its own errors with these)."""
    if not language.valid(lang):
        raise HTTPException(404, "Unknown language.")
    return catalog.bundle(lang)


@app.get("/conversations")
def list_conversations(caller: Caller = Depends(require_chat_caller)):
    """List the caller's conversations - returns list of {id, title} objects."""
    try:
        if not caller.prefix:
            return memory.list_conversations()
        return [{**c, "id": c["id"][len(caller.prefix):]} for c in memory.list_conversations(caller.prefix)]
    except Exception as e:
        log.error(f"Error listing conversations: {e}", exc_info=True)
        # Return empty list on error instead of raising
        return []


@app.get("/conversations/{conv_id}", response_model=ConversationHistory)
def get_conversation(conv_id: str, caller: Caller = Depends(require_chat_caller)):
    """Get full conversation history."""
    try:
        return ConversationHistory(id=conv_id, messages=memory.get_messages(_conv(caller, conv_id), who=identity.resolve(caller.email)))
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error getting conversation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")


@app.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: str, caller: Caller = Depends(require_chat_caller)):
    """Delete a conversation."""
    try:
        removed = memory.delete(_conv(caller, conv_id))
        if removed:
            _audit(caller.email or "service token", "conversation.delete", _conv(caller, conv_id))
        return {"status": "deleted", "conv_id": conv_id}
    except Exception as e:
        log.error(f"Error deleting conversation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
