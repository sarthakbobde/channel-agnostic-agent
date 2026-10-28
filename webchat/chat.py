"""Web chat (Chainlit). People sign in with the email and password from the admin center; Viewers choose their
password from the one-time link an Admin gives them (/set-password). Run from this folder:
    chainlit run chat.py --port 8501
RAG_BACKEND_URL points at the API; CHAINLIT_AUTH_SECRET signs the sign-in cookie."""
import html
import logging
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



@cl.data_layer
def history():
    return backend.BackendDataLayer(config.ui.name)


@cl.password_auth_callback
async def sign_in(username: str, password: str):
    return await backend.sign_in(username, password)


@cl.on_chat_resume
async def resume(thread):
    pass                                    # the conversation itself lives in the API; nothing to restore here


@cl.on_message
async def answer(message: cl.Message):
    token = backend.token_for(cl.user_session.get("user"))
    status, body = await backend.ask(token, cl.context.session.thread_id, message.content)
    if status == 200:
        await cl.Message(content=backend.with_sources(body.get("answer", ""), body.get("sources", []))).send()
    elif status in (401, 503):
        await cl.ErrorMessage(content="Your session has ended. Sign out (top right) and sign in again.").send()
    elif status in (0, 429):
        await cl.ErrorMessage(content=body.get("detail", "Please try again.")).send()
    else:
        await cl.ErrorMessage(content=f"Something went wrong on our side (error {status}). Please try again.").send()


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
