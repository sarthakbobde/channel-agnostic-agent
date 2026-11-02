"""Web chat (Chainlit). People sign in with the email and password from the admin center; Viewers choose their
password from the one-time link an Admin gives them (/set-password). Run from this folder:
    chainlit run chat.py --port 8501
RAG_BACKEND_URL points at the API; CHAINLIT_AUTH_SECRET signs the sign-in cookie."""
import asyncio
import html
import logging
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")    # local runs; Cloud Run sets the environment itself

# The imports below read that environment (backend.BACKEND_URL), so they come after it.
import chainlit as cl  # noqa: E402
from chainlit.config import config  # noqa: E402
from chainlit.server import app as server  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import HTMLResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import backend  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)       # not a line per API call
TYPING_SECONDS = 1.5           # longest time an answer takes to type out, however long it is
TYPING_STEP = 0.03             # pause between words for a short answer



@cl.data_layer
def history():
    return backend.BackendDataLayer(config.ui.name)


@cl.password_auth_callback
async def sign_in(username: str, password: str):
    return await backend.sign_in(username, password)


def _suggestions(items: list[dict], title: str) -> list:
    """Suggested questions with a live payload, shown under the answer as full-width rows (public/elements/Suggestions.jsx).
    A click calls the "caa_suggest" action below; the API checks the signed payload."""
    live = [{"text": s["text"], "payload": s["payload"]} for s in items if s.get("payload")]
    return [cl.CustomElement(name="Suggestions", props={"title": title, "items": live}, display="inline")] if live else []


@cl.on_chat_resume
async def resume(thread):
    # The conversation itself lives in the API. The last answer's suggestions come back as buttons until they expire.
    live = backend.resume_suggestions(thread["id"])
    if live:
        token = backend.token_for(cl.user_session.get("user"))
        texts = await backend.texts(token, cl.user_session.get("lang") or "")
        await cl.Message(content="", elements=_suggestions(live, texts["suggested"])).send()


@cl.action_callback("caa_suggest")
async def suggestion_clicked(action: cl.Action):
    await cl.Message(content=action.payload.get("text", ""), type="user_message").send()
    token = backend.token_for(cl.user_session.get("user"))
    status, body = await backend.act(token, cl.context.session.thread_id, action.payload.get("payload", ""))
    await _show(token, status, body)


@cl.on_message
async def answer(message: cl.Message):
    token = backend.token_for(cl.user_session.get("user"))
    status, body = await backend.ask(token, cl.context.session.thread_id, message.content)
    await _show(token, status, body)


async def _type(msg: cl.Message, text: str) -> None:
    """Type the answer out word by word. It is the finished answer, already checked by the API (guardrails, personal data,
    sources): nothing unchecked is ever shown, this only makes a long answer easier to follow as it appears."""
    pieces = re.findall(r"\S+\s*|\s+", text) or [text]
    pause = min(TYPING_STEP, TYPING_SECONDS / len(pieces))
    for piece in pieces:
        await msg.stream_token(piece)
        await asyncio.sleep(pause)


async def _show(token: str, status: int, body: dict) -> None:
    if status == 200 and body.get("language"):
        cl.user_session.set("lang", body["language"])          # the person's language, for the texts below
    texts = await backend.texts(token, cl.user_session.get("lang") or "")
    if status == 200:
        items = body.get("suggestions") or []
        content = (backend.with_sources(body.get("answer", ""), body.get("sources", []), body.get("language", ""), texts["sources"])
                   + backend.suggestions_text([s for s in items if not s.get("payload")], texts["suggested"]))
        msg = cl.Message(content="", **({"id": backend.step_id(body["answer_id"], "")} if body.get("answer_id") else {}))
        await _type(msg, content)
        msg.elements = _suggestions(items, texts["suggested"])     # the suggestions appear once the answer is complete
        await msg.send()
    elif status in (400, 403, 409, 410):                     # a suggestion button that is not valid, not yours, used or expired
        await cl.ErrorMessage(content=body.get("detail", texts["web_try_again"])).send()
    elif status in (401, 503):
        await cl.ErrorMessage(content=texts["web_session_ended"]).send()
    elif status in (0, 429):
        await cl.ErrorMessage(content=body.get("detail", texts["web_try_again"])).send()
    else:
        await cl.ErrorMessage(content=texts["web_error"].replace("{status}", str(status))).send()


# ---------- choose a password (opened from the one-time link) ----------
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">
<title>Choose your password</title><link rel="icon" href="/favicon"><style>
:root{color-scheme:light dark;--bg:#f8fafc;--card:#fff;--text:#0f172a;--muted:#64748b;--line:#e2e8f0;--accent:#4f46e5;
--ok:#047857;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#0f172a;--card:#1e293b;--text:#f1f5f9;--muted:#94a3b8;--line:#334155;
--accent:#818cf8;--ok:#34d399;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;background:var(--bg);
color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}
h1{font-size:20px;margin:0 0 4px}p{margin:0 0 18px;color:var(--muted)}label{display:block;font-weight:600;margin:14px 0 6px}
input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:transparent;color:inherit;font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button,a.btn{display:block;width:100%;margin-top:20px;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;
font:inherit;font-weight:600;text-align:center;text-decoration:none;cursor:pointer}
.msg{margin:0 0 4px;font-weight:600}.ok{color:var(--ok)}.bad{color:var(--bad)}
</style></head><body><main>{body}</main></body></html>"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.replace("{body}", body), status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _form(token: str, error: str = "") -> str:
    return (f'<h1>Choose your password</h1><p>At least 12 characters. You will use it with your email to sign in.</p>'
            + (f'<p class="msg bad" role="alert">{html.escape(error)}</p>' if error else "")
            + f'<form method="post"><input type="hidden" name="token" value="{html.escape(token)}">'
            '<label for="pw">New password</label><input id="pw" name="password" type="password" minlength="12" '
            'autocomplete="new-password" required><label for="again">Repeat password</label>'
            '<input id="again" name="again" type="password" minlength="12" autocomplete="new-password" required>'
            '<button type="submit">Save password</button></form>')


async def set_password_page(request: Request) -> HTMLResponse:
    if request.method == "GET":
        token = request.query_params.get("token", "")
        if not token:
            return _page('<h1>Link incomplete</h1><p>Open the full link your administrator sent you.</p>', 400)
        return _page(_form(token))
    form = await request.form()
    token, pw, again = str(form.get("token", "")), str(form.get("password", "")), str(form.get("again", ""))
    if pw != again:
        return _page(_form(token, "The two passwords do not match."), 400)
    ok, text = await backend.set_password(token, pw)
    if not ok:
        return _page(_form(token, text), 400)
    return _page('<h1>Password saved</h1><p class="msg ok">You can now sign in with your email and the new password.</p>'
                 '<a class="btn" href="/login">Sign in</a>')


# Chainlit serves its app for every unknown path, so this page goes ahead of that catch-all route.
server.router.routes.insert(0, Route("/set-password", set_password_page, methods=["GET", "POST"]))
