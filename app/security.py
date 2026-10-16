"""Authentication for the chat API (/chat, /conversations...).

Two ways in, both sent as `Authorization: Bearer <token>`:
  * a chat session token from /chat-auth/login (a person: their conversations are private to them);
  * the service token CHAT_API_TOKEN, an optional machine credential for scripts (an unknown Viewer,
    sees conversations without per-person scoping).
If neither is possible the API stays locked (503) rather than open: it fails closed.

Chat apps never use this path: Slack, Teams and Google Chat call the answering core directly,
and their webhooks are authenticated by the platform's own signature.
"""
import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException

from app import chat_auth
from app.config import settings


@dataclass(frozen=True)
class Caller:
    email: str = ""
    prefix: str = ""             # conversation-id prefix that scopes a person's conversations ("" = service)
    service: bool = False


def require_chat_caller(token: str | None = Depends(chat_auth.bearer_token)) -> Caller:
    user = chat_auth.user_from_token(token)
    if user is not None:
        return Caller(user.email, user.prefix)
    expected = settings.chat_api_token
    if not expected:
        raise HTTPException(503, "The chat API is locked: sign in, or set CHAT_API_TOKEN on the server.")
    if token is None or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(401, "Authentication required.", headers={"WWW-Authenticate": "Bearer"})
    return Caller(service=True)
