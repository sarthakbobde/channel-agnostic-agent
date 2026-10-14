# Channel Agnostic Agent: Progress Log

Last updated: 2026-10-14

## Goal
**Channel Agnostic Agent** is a RAG assistant that plugs into chat apps (Slack, Teams, Google Chat) with **no code
changes per channel**. The same core answers every channel; each platform gets a thin
adapter, and admins manage channels and their credentials from a web page.

## Architecture

```
Slack ──┐                        ┌─ verify request
Teams ──┼─► channel adapter ────►│─ parse → Envelope
GChat ──┘   (app/channels/)      └─ format reply
                 ▲                   │
                 │ start / stop /    ▼
        ChannelManager      chat_service.chat()  → route → retrieve (Chroma) → answer (LLM)
                 ▲          memory (per conversation) · knowledge base (data/docs)
                 │ settings + encrypted secrets
   Admin page / API  ──►  admin database (SQLite)  ·  audit log
   (/admin, local only)
```

- **Envelope** (`app/channels/base.py`): platform-neutral message
  `{channel, workspace, room, thread, user, text}`. `conv_id = channel-workspace-room-thread`,
  so every Slack thread / Teams chat gets its own memory.
- **Adapter interface** (`base.py`): each adapter declares its settings (`SPEC`, a list of
  `Field`s with a `secret` flag), validates a config, offers `test()`, and implements either
  `start()`/`stop()` (Slack, Socket Mode) or `handle_webhook()` (Teams, Google Chat).
- **ChannelManager** (`manager.py`): starts, replaces and stops adapters at runtime. A new
  adapter is started before the old one stops, so a bad credential never takes down a
  working channel. Webhook routes are mounted once and forward to the running adapter.
- **Channel store** (`store.py`): channel settings in SQLite, secrets encrypted (Fernet) with
  a master key from `APP_SECRET_KEY`, every change in an audit log.
- **Single entry point**: `app/channels/core.py::handle()` (never raises, returns a friendly
  error reply).
- **Config source**: the admin database. On first start the legacy `channels.yaml` + `.env`
  values are imported once. Without `APP_SECRET_KEY` the server falls back to `channels.yaml`.

## What was done

### 1. Code review of the original project
Found and fixed:
- Dockerfile copied files that don't exist (`ask.py`, `chainlit.md`) and had no `curl` for
  its health check.
- A brand-new chat returned 404 and showed a warning in the Streamlit UI.
- Streamlit rendered sources wrongly (raw dicts) and never showed them for saved history.
- `async def` handlers ran blocking LLM/Chroma/file calls → now plain `def`.
- Errors leaked internal messages to clients → now a generic message.
- Duplicate `return` in `memory.py`, bare `except:` in the UI, obsolete compose `version`.

### 2. Channel framework + Slack adapter
- Slack uses **Socket Mode** (outbound websocket, so no public HTTPS URL needed).
- Behaviour: answers DMs and `@mentions`, replies in a thread, ignores bot messages,
  converts Markdown to Slack formatting, appends a *Sources* line, optional
  `allowed_channels`.

### 3. Slack app setup (done in the Sysfore workspace)
- Slack app (now named **Channel Agnostic Agent**, originally "RAG Assistant") created from a manifest: Socket Mode on, bot scopes
  `app_mentions:read`, `chat:write`, `im:history`, `im:read`; events `app_mention`,
  `message.im`; Messages tab enabled. App-level token (`connections:write`), app installed.
- **Verified**: a DM to the bot in Slack got a cited answer.

### 4. Local run (instead of Docker)
- Python 3.11 virtualenv at `C:\Users\sarthak.bobde\rag-venv` (kept outside the project
  because the long OneDrive path breaks pip on Windows).
- `data/docs/HR-Policy.pdf` indexed into local Chroma (41 chunks).
- LLM switched from 9Router to **Gemini** via its OpenAI-compatible endpoint (no code change,
  only `.env`): model `gemini-3.8-flash` (`gemini-2.5-flash` is retired for new accounts).

### 5. Teams and Google Chat adapters (built, not yet connected)
- `teams.py`: webhook `POST /channels/teams/messages`, Bot Framework authentication,
  immediate acknowledgement then a proactive reply, optional `allowed_tenants`.
- `gchat.py`: webhook `POST /channels/gchat/events`, verifies Google's signed token
  (`project_number` or `endpoint_url`), supports classic and Workspace add-on events,
  replies synchronously with a 25 s timeout guard, optional `allowed_spaces`.
- Tested offline (forged/missing token → 401, wrong content type → 415, event shapes,
  timeout). **Neither has been tested against the real platform.**
- Setup steps: `TEAMS_SETUP.md`, `GCHAT_SETUP.md`.

### 6. Start script and project move
- `start.ps1`: `.\start.ps1` (server), `-Tunnel` (Cloudflare quick tunnel, prints the
  endpoint URL), `-Ingest` (re-index `data\docs`).
- Project moved to `...\Developement\AI\rag-assistant`; published to GitHub
  (`sarthakbobde/channel-agnostic-agent`, public; renamed from `rag-assistant`).

### 7. Admin work (phases 0 to 4)
Goal: admins manage channels and credentials without editing files or restarting.

| Phase | Delivered |
|---|---|
| **0. Adapter refactor** | `SPEC` per adapter, `validate()`, `test()`, `ChannelManager` (hot start/replace/stop), one dynamic webhook route per type |
| **1. Storage and secrets** | SQLite admin database; `SecretStore` (Fernet, master key from `APP_SECRET_KEY`, rotation via comma-separated keys); write-only secrets; audit log; one-time import of `channels.yaml` + `.env` |
| **2. Admin API and sign-in** | `/admin/api`: local password sign-in (scrypt), signed HttpOnly SameSite=Strict cookie, CSRF header, login lockout, channels CRUD + test, audit, admins |
| **3. Runtime manager** | Folded into phase 0 (hot reload and test connection) |
| **4. Admin page** | `/admin`: sign-in, channels (forms generated from adapter fields), test connection, enable/disable/delete, audit log, admins, account |

Design rules enforced in code and tests:
- **Saving validates first.** A channel is built and started before anything is saved; on
  failure the stored settings and the running connection are untouched.
- **Secrets are write-only.** Never returned by the API, never in the audit log, scrubbed from
  error messages, encrypted at rest (checked against the raw database files).
- **Local only.** Admin routes refuse requests with proxy/tunnel headers or from another
  machine, because the same server is exposed to Teams/Google Chat through a tunnel.
- **Strict page security.** CSP without inline script/style, no framing, `nosniff`,
  `no-store`; the page builds content with DOM calls only (a test forbids HTML strings).
- **CORS closed** unless origins are listed in `CORS_ORIGINS` (was: every origin).
- 55 automated tests pass (`pytest tests`). The page was verified in a browser against a
  throwaway server and database.

See `ADMIN.md` for setup and use.

## How to run

```powershell
.\start.ps1            # start the server on 127.0.0.1:8000
.\start.ps1 -Ingest    # re-index data\docs first
.\start.ps1 -Tunnel    # also expose a public HTTPS URL (for Teams / Google Chat)
```

First-time admin setup:

```powershell
& "$env:USERPROFILE\rag-venv\Scripts\python.exe" -m app.secretstore generate   # put the key in .env as APP_SECRET_KEY
& "$env:USERPROFILE\rag-venv\Scripts\python.exe" -m app.admin.cli create-admin you@sysfore.com
```

Then open `http://127.0.0.1:8000/admin`. Check the server: `http://127.0.0.1:8000/health`.
Add documents = drop `.pdf/.md/.txt` into `data/docs` and re-run ingest.
Run the tests: `python -m pytest tests` (set `CHROMA_PATH`, `CHAT_DIR` to temp folders).

## Key files

| File | Purpose |
|---|---|
| `app/api.py` | FastAPI app: `/chat`, `/conversations`, `/health`; mounts channel, admin API and admin page routes |
| `app/chat_service.py`, `rag.py`, `retriever.py` | Routing, retrieval, grounded answer |
| `app/channels/` | `base` (Envelope, Field, Adapter), `manager`, `store`, `core`, and one module per platform |
| `app/admin/` | `api.py` (admin API), `auth.py` (passwords, sessions, limiter), `users.py`, `cli.py`, `ui.py` + `ui/` (the page) |
| `app/db.py`, `app/secretstore.py` | Admin database models; encryption of stored secrets |
| `channels.yaml` | Legacy channel config; imported once into the admin database |
| `.env` / `.env.example` | Settings (`.env` is not committed or copied by tooling; recreate it) |
| `start.ps1` | Local start script |
| `ADMIN.md`, `TEAMS_SETUP.md`, `GCHAT_SETUP.md` | Admin guide; platform setup steps |
| `tests/` | 55 tests: channels, store/encryption, admin API, admin page |
| `docs/` | Architecture and one-pager PDFs and their HTML sources |

## Open items / next steps
1. **Back up `APP_SECRET_KEY`.** It is in `.env`. If it is lost, stored secrets cannot be
   decrypted and must be re-entered.
2. **Create the first admin** with the CLI above (not yet done on the live database).
3. **Rotate secrets**: the Gemini key was pasted in chat; Slack tokens passed through tool
   output. Regenerate if that matters for your policy. After that, the old Slack token lines in
   `.env` are no longer needed (the database holds the working copy).
4. **LLM credit**: the Gemini key ran out of prepaid credit (HTTP 402), so the bot currently
   cannot answer until credit is added or another key/provider is configured.
5. **Teams / Google Chat**: create the Azure Bot / Chat app, then enter the credentials on the
   admin page and run `.\start.ps1 -Tunnel`. Teams needs sideloading to be allowed; Google
   Chat needs Google Workspace.
6. **Move `chroma_db` and `chats` out of OneDrive** (`CHROMA_PATH`, `CHAT_DIR`) to avoid SQLite
   sync locks; re-ingest once. Keep `admin_data/` out of OneDrive too (`ADMIN_DB_PATH`).
7. **Docker is not wired for the admin work**: `docker-compose.yml` has no `APP_SECRET_KEY` or
   admin-data volume, and the Docker path as a whole has not been tested.
8. **Admin gaps**: sign-in is local only (Entra ID planned); sessions end after 8 hours, on
   password change or on a key change but cannot be revoked individually; `/docs` is still
   reachable through a tunnel; the page was browser-tested for the main flows only (not
   delete, add-admin, change-password or phone layouts).
9. **Hardening before real use**: authentication on `/chat` and `/conversations`, rate limits
   on chat, pinned dependency versions, a real `requirements.txt`, and a service or Docker
   deployment so the server survives restarts.
10. **Upcoming (shown greyed out in the admin sidebar, not started)**:
    - **Documents**: upload and per-document access (who can add, who can see which files).
    - **Chunking strategy**: today fixed 900/150 recursive; planned fixed size, per page, by
      heading, semantic.
    - **Vector store**: today hard-wired to ChromaDB; planned pgvector, Qdrant, Azure AI Search.
    - **Retrieval strategy**: today vector similarity with a score threshold; planned hybrid
      keyword + vector, re-ranker, query rewriting.
    - **LLM provider and model**: today one OpenAI-compatible model (Gemini via its
      OpenAI-compatible endpoint); planned Claude, Azure, Ollama, a different model per step
      (routing vs answering), and a fallback if the primary fails or runs out of credit.
    - Changing chunking, embeddings or the vector store means a full rebuild into a new
      versioned index (old one kept for rollback), never a mix of strategies in one index.
11. **Later still**: multiple knowledge bases, tracing and evaluation.
