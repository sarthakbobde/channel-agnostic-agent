"""FastAPI REST API for Channel Agnostic Agent."""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import channels, chat_service, memory
from app.admin.api import router as admin_router
from app.admin.ui import router as admin_ui_router
from app.config import settings

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
    yield
    channels.stop_all()
    log.info("Channel Agnostic Agent shutdown")


app = FastAPI(
    title="Channel Agnostic Agent",
    description="Retrieval-Augmented Generation chatbot engine",
    version="0.1.0",
    lifespan=lifespan,
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
app.include_router(admin_router)
app.include_router(admin_ui_router)


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(request: MessageRequest):
    """Chat endpoint - returns answer with sources."""
    try:
        answer_iter, meta = chat_service.chat_stream(request.message, request.conv_id)
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


@app.get("/conversations")
def list_conversations():
    """List all conversations - returns list of {id, title} objects."""
    try:
        conversations = memory.list_conversations()
        # Return ID and title
        return conversations
    except Exception as e:
        log.error(f"Error listing conversations: {e}", exc_info=True)
        # Return empty list on error instead of raising
        return []


@app.get("/conversations/{conv_id}", response_model=ConversationHistory)
def get_conversation(conv_id: str):
    """Get full conversation history."""
    try:
        messages = memory.get_messages(conv_id)
        return ConversationHistory(
            id=conv_id,
            messages=messages,
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Error getting conversation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")


@app.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    """Delete a conversation."""
    try:
        memory.delete(conv_id)
        return {"status": "deleted", "conv_id": conv_id}
    except Exception as e:
        log.error(f"Error deleting conversation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal error")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
