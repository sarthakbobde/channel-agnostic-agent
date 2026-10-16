"""FastAPI REST API for Channel Agnostic Agent."""
import logging
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import channels, chat_auth, chat_service, identity, memory
from app.jobs import scheduler
from app.providers import seed as provider_seed
from app.knowledge import documents as knowledge_documents, kbs as knowledge_kbs
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
    sources: list[dict]
    searched: bool
    query: str = ""


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
    scheduler.start()
    if settings.admin_port:
        admin_listener.start()
    yield
    admin_listener.stop()
    knowledge_documents.shutdown()
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

# CORS: no browser origin is trusted unless listed in CORS_ORIGINS. (Streamlit and the chat
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
    """Health check endpoint."""
    return {"status": "ok"}


# API documentation: reachable only from this machine, never through a tunnel or proxy.
@app.get("/openapi.json", include_in_schema=False)
def openapi_schema(request: Request):
    check_local(request, allow_remote=False)
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def swagger_ui(request: Request):
    check_local(request, allow_remote=False)
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} API")


def _conv(caller: Caller, conv_id: str) -> str:
    """A person's conversations live under their own prefix, so nobody can read or delete another's."""
    return caller.prefix + conv_id


@app.post("/chat", response_model=ChatResponse)
def chat(request: MessageRequest, caller: Caller = Depends(require_chat_caller)):
    """Chat endpoint - returns answer with sources."""
    who = identity.resolve(caller.email)
    token = identity.bind(who)
    try:
        kb_ids = chat_auth.web_kb_ids()
        answer_iter, meta = chat_service.chat_stream(request.message, _conv(caller, request.conv_id),
                                                     **({"kb_ids": kb_ids} if kb_ids else {}))
        answer = "".join(answer_iter)

        return ChatResponse(
            answer=answer,
            sources=meta.get("sources", []),
            searched=meta.get("searched", False),
            query=meta.get("query", ""),
        )
    except Exception as e:
        log.error(f"Chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")
    finally:
        identity.unbind(token)


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
        return ConversationHistory(id=conv_id, messages=memory.get_messages(_conv(caller, conv_id)))
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error getting conversation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")


@app.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: str, caller: Caller = Depends(require_chat_caller)):
    """Delete a conversation."""
    try:
        memory.delete(_conv(caller, conv_id))
        return {"status": "deleted", "conv_id": conv_id}
    except Exception as e:
        log.error(f"Error deleting conversation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
