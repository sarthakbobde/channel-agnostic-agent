# Channel Agnostic Agent

A knowledge assistant that answers employees' questions from the organisation's own documents, with file and page
citations, inside the chat tools people already use: **Slack, Microsoft Teams, Google Chat and a Web UI**. One answering
core serves every channel; each platform is a thin adapter, so adding a channel needs no change to the core.

Everything is configured in the **admin center** (`/admin`): channels, AI providers, knowledge bases and documents,
prompts, guardrails, people and retention. Changes apply within seconds, with no code changes or restarts.

---

## Contents

- [What it does](#what-it-does)
- [The admin center](#the-admin-center)
- [Quick start (Windows)](#quick-start-windows)
- [Quick start (Docker)](#quick-start-docker)
- [First-time setup](#first-time-setup)
- [Configuration (`.env`)](#configuration-env)
- [Chat API](#chat-api)
- [Security model](#security-model)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Tests](#tests)
- [Deploying](#deploying)
- [Documentation](#documentation)

---

## What it does

| Area | Capabilities |
|---|---|
| **Channels** | Slack (Socket Mode, no public URL), Microsoft Teams and Google Chat (HTTPS webhooks), a Web UI (Chainlit). Added, tested and switched on in the admin center; a bad credential never stops a working channel. |
| **AI providers** | Anthropic, Google Gemini, OpenAI, Azure OpenAI, Grok (xAI), DeepSeek and any OpenAI-compatible endpoint. A preferred model plus a fallback on another provider that takes over on credit, rate-limit, server or timeout errors. |
| **Knowledge bases** | Several per organisation, each owned by a department and bound to channels. Chunking strategies (fixed size, per page, by heading, semantic) with a preview; embedding models (local MiniLM, local multilingual, OpenAI, Azure OpenAI, Cohere); vector stores (Chroma, pgvector, Azure AI Search). Changes rebuild into a new versioned index with one-click rollback. |
| **Document access** | Each document is visible to everyone, selected groups or selected people. Enforced inside the search, so blocked content never reaches the model. |
| **Prompts** | Locked base prompts (Admin) plus a prompt per department (its Editors). Draft, test, publish, version history and rollback. |
| **Guardrails** | Question length, prompt injection, toxic language, banned topics per department, personal-data masking on questions and redaction on answers, protection of the hidden instructions. |
| **Conversations** | Stored in the database (personal data masked), long chats summarised in the background, retention per department, audited export. |
| **Monitoring** | Home dashboard, usage and cost per provider/model/department/knowledge base, guardrail and knowledge-base reports, alerts (banner, webhook, email), a full audit log. |
| **People** | Admin, Editor and Viewer roles; departments; groups; one-time set-password links; profile photos; sign in with Microsoft or Google (admin center). |

---

## The admin center

Open `http://127.0.0.1:8000/admin` on the server. The side menu follows the order an admin works in, and collapses to
icons with the ☰ button.

| Menu | Screens |
|---|---|
| **Home** | Greeting, a *Finish setting up* checklist checked against the real configuration, this week's numbers against last week's, channel status and *Needs your attention*. |
| **Settings** | **Channels** (official platform logos, status, credentials, test, enable) · **LLM providers** (which model answers, connections, allowed providers) · **KB configuration** (create knowledge bases; splitting, search model, search storage, rebuilds, who can ask) · **RAG settings** (retrieval, retention, limits, alert notifications) · **Guardrails** · **Sign-in** (Sign in with Microsoft / Google) |
| **User management** | **Users** (search and filters, roles, departments, how each person signs in, password links) · **Groups** · **Departments** (retention, banned topics, Editors) |
| **Input** | **Prompts** (prompt list with Edit, Test and History tabs) · **KB documents** (drag-and-drop upload, file list with type icons, who can see each file) |
| **Monitoring** | **Reports** · **Audit log** · **Conversations** · **Usage and cost** |
| **Account** (your name, top right) | Profile photo, department and groups, light/dark theme, accent colour, password |

Design notes: short titles with explanations behind an **(i)**; one item open at a time in long lists; a strict
Content-Security-Policy (no inline script or style, no external requests), so icons and logos are drawn with DOM calls.

Roles: **Admin** sees everything. **Editor** manages the knowledge bases, documents, groups, prompts, departments and
usage of the departments they edit. **Viewer** only asks questions (no admin center; can use the Web UI).

---

## Quick start (Windows)

Requires **Python 3.11** (the pinned packages are tested on it) and git. About 3 GB of disk on first install.

```powershell
git clone https://github.com/sarthakbobde/channel-agnostic-agent.git
cd channel-agnostic-agent

# 1. Python environment, outside OneDrive (long paths break pip there)
py -3.11 -m venv $env:USERPROFILE\rag-venv
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m pip install --upgrade pip
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m pip install -e .

# 2. Settings: copy the template, then add the two required secrets
Copy-Item .env.example .env
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m app.secretstore generate                               # -> APP_SECRET_KEY
& $env:USERPROFILE\rag-venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"      # -> CHAT_API_TOKEN

# 3. Create the first admin (asks for a password at a hidden prompt)
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m app.admin.cli create-admin you@yourcompany.com

# 4. Start, then open http://127.0.0.1:8000/admin
.\start.ps1
```

- `.\start.ps1 -Tunnel` also opens a public HTTPS address for testing Teams and Google Chat (needs `cloudflared`); the admin
  page then moves to its own local port (`http://127.0.0.1:8001/admin`) so it is never tunnelled.
- Optional Web UI: add `CHAINLIT_AUTH_SECRET` to `.env` (`chainlit create-secret`), then `cd webchat; chainlit run chat.py --port 8501` and open `http://localhost:8501`.
- **Back up `APP_SECRET_KEY`.** Without it the stored credentials cannot be decrypted and must be entered again.
- The database schema is managed by Alembic and upgrades itself when the server starts.

macOS/Linux: the same steps with `python3.11 -m venv .venv && source .venv/bin/activate`, and
`python -m uvicorn app.api:app --host 127.0.0.1 --port 8000` instead of `start.ps1`. Full detail: [RUNNING.md](RUNNING.md).

## Quick start (Docker)

```bash
cp .env.example .env              # fill in APP_SECRET_KEY and CHAT_API_TOKEN (see above)
docker compose up -d --build      # first build takes several minutes
docker compose exec rag python -m app.admin.cli create-admin you@yourcompany.com
curl http://127.0.0.1:8000/health
```

The chat API is on `http://127.0.0.1:8000`; the admin center runs on its own port, `http://127.0.0.1:8090/admin`. Both are
published on this machine only. Data lives in named volumes (admin database, vectors, uploaded files, models, chats);
`docker compose down -v` deletes them.

---

## First-time setup

The **Home** screen shows this as a checklist and ticks each step off as it is done.

1. **LLM providers**: tick a provider under *Allowed providers*, **Add connection** with its key (tested before anything is
   saved), then choose the *Preferred* and, ideally on another provider, the *Fallback* model.
2. **Users** and **Departments**: add people and share the one-time set-password link shown on screen.
3. **KB configuration**: add a knowledge base and set how its documents are split and searched.
4. **KB documents**: upload files, wait for *Indexed*, and choose who may see each one.
5. **Channels**: enter and test the credentials, choose the knowledge bases, switch on. Teams and Google Chat need a public
   HTTPS address; see [PLATFORM_RUNBOOK.md](PLATFORM_RUNBOOK.md).
6. **Prompts** and **Guardrails**: review the defaults.

Step-by-step for every provider, embedding model, vector store and channel: [USER_MANUAL.md](USER_MANUAL.md).

---

## Configuration (`.env`)

Most configuration lives in the admin center. `.env` holds what the server needs before it can read its database.

| Name | Needed | Meaning |
|---|---|---|
| `APP_SECRET_KEY` | **yes** | Master key that encrypts every stored secret. Back it up. Rotation: [ADMIN.md](ADMIN.md) |
| `CHAT_API_TOKEN` | **yes** | Bearer token for `/chat` and `/conversations` (service callers; people sign in instead) |
| `ADMIN_EMAIL_DOMAIN` | if not `sysfore.com` | Company domain people's emails must be on |
| `DATABASE_URL` / `ADMIN_DB_PATH` | optional | PostgreSQL (`postgresql+psycopg://user:pass@host:5432/db`) instead of the SQLite file |
| `ADMIN_PORT`, `ADMIN_HOST`, `ADMIN_ALLOW_REMOTE`, `ADMIN_REQUIRE_HTTPS`, `ADMIN_PUBLIC_URL` | optional | Run the admin center on its own port, and let Editors reach it over HTTPS ([DEPLOYMENT.md](DEPLOYMENT.md)) |
| `CHROMA_PATH`, `DOCS_DIR`, `CHAT_DIR`, `MODELS_DIR` | optional | Where vectors, uploaded files, chats and downloaded models are kept |
| `MAX_UPLOAD_MB`, `WEB_UI_URL`, `CORS_ORIGINS` | optional | Upload limit, where Viewers' password links point, allowed browser origins |
| `NINEROUTER_*`, `LLM_MODEL`, `SLACK_*`, `TEAMS_*`, `GCHAT_*`, `TOP_K`, `CHUNK_*` … | first start only | Seeds imported once into the database; afterwards edit them in the admin center |

---

## Chat API

Every channel calls the same core. The HTTP API is for the Web UI and integrations; it needs the service token.

```bash
TOKEN=...   # CHAT_API_TOKEN from .env
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/chat -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"message": "How many days of annual leave do I get?", "conv_id": "demo-1"}'
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/conversations
```

The answer comes back with its sources (file, page, score). Webhooks for the chat platforms are
`/channels/teams/messages` and `/channels/gchat/events`; both verify the platform's signed token.

---

## Security model

- **Secrets** (channel tokens, API keys, store keys, mail passwords) are encrypted at rest with `APP_SECRET_KEY`, write-only in
  the API and UI, and scrubbed from logs and the audit log.
- **Admin center**: local-only by default; signed HttpOnly session cookies, CSRF tokens on every change, sign-in lockout, role
  checks on every request (default-deny), strict CSP. It refuses requests that arrive through a tunnel or proxy unless
  remote access over HTTPS is switched on. Optional Microsoft / Google sign-in (OpenID Connect with PKCE, state and nonce;
  one Entra tenant or one Workspace domain) only signs in people already under Users; email and password stay as a fallback.
- **Chat API**: bearer token required; per-person rate limit; `/docs` only from the server itself.
- **Document access** is applied inside the vector search, so restricted passages never reach the model.
- **Personal data** is masked before it reaches the model, the conversation history or any log.
- **Profile photos** accept PNG, JPEG or WebP only, checked by their bytes (SVG refused), up to 512 KB.
- **Audit log**: every sign-in, configuration change, prompt version, document change, export and retention run.

---

## Architecture

```
 Slack ──(Socket Mode)──┐
 Teams ──(HTTPS)────────┤      ┌──────────────────────── FastAPI server ────────────────────────┐
 Google Chat ─(HTTPS)───┼────► │ channel adapters ─► answering core                              │
 Web UI ──(chat API)────┘      │                      ├─ guardrails (input / output)             │
                               │                      ├─ router + retrieval ─► vector store       │──► Chroma / pgvector / Azure AI Search
 Admin center (/admin) ──────► │ admin API            ├─ prompts (base + department)             │
                               │ (roles, CSRF, audit) └─ LLM with fallback ─────────────────────│──► Anthropic / OpenAI / Azure OpenAI / Gemini / Grok / DeepSeek
                               │ scheduler: retention, store health, summaries                   │
                               └──────────────── SQLite or PostgreSQL (settings, people, prompts, │
                                                  conversations, usage, audit, encrypted secrets) ┘
```

---

## Project layout

```
app/
  api.py              FastAPI app: chat API, channel webhooks, admin mounting
  chat_service.py     The answering core every channel calls
  rag.py, retriever.py, llm.py, memory.py, summaries.py, guardrails.py, prompts.py
  channels/           Slack, Teams, Google Chat, Web UI adapters and the channel manager
  providers/          LLM provider adapters, approval and model settings
  knowledge/          Knowledge bases, documents, chunkers, embeddings, vector store adapters, index versions, access
  admin/              Admin API routers, roles, sessions, CLI
  admin/ui/           The admin center (index.html, admin.js, admin.css; no build step, no framework)
  jobs/               Scheduled jobs (retention, store health)
  migrations/         Alembic migrations (upgrade automatically on start)
webchat/             Optional Web UI (Chainlit): chat.py, backend.py, theme and logos
tests/                Unit, API, security, migration and end-to-end scenario tests
docs/                 Architecture, one-pager, BRD-vs-build
```

---

## Tests

They use temporary data and never touch your real database.

```powershell
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m pip install -e ".[dev]"
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m pytest                                    # SQLite
$env:TEST_DB = "postgres"; & $env:USERPROFILE\rag-venv\Scripts\python.exe -m pytest          # PostgreSQL in Docker
```

Tests marked `live` need a real Microsoft tenant or Google Workspace and are skipped unless run with `-m live`.

---

## Deploying

- Put an HTTPS reverse proxy in front and forward only the public routes (`/health`, `/chat`, `/conversations`,
  `/chat-auth/`, `/channels/`); keep the admin center on its own port. Example nginx config and go-live checks:
  [DEPLOYMENT.md](DEPLOYMENT.md).
- Run **one** server process: the scheduler and the per-person rate limit live in memory.
- Containers (for example Azure Container Apps): one replica, always on, persistent storage for the database, uploaded files
  and vectors (or PostgreSQL + pgvector / Azure AI Search), and `APP_SECRET_KEY` from a secret store such as Key Vault.

---

## Documentation

| Read | For |
|---|---|
| [USER_MANUAL.md](USER_MANUAL.md) | Configuring everything: providers (Azure OpenAI and others), embeddings, vector stores, channels, people, prompts, guardrails, settings |
| [RUNNING.md](RUNNING.md) | Set up, run, test, update, push |
| [ADMIN.md](ADMIN.md) | The admin center in depth, master key rotation, security model |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Exposing the agent safely: tunnel, TLS, reverse proxy |
| [PLATFORM_RUNBOOK.md](PLATFORM_RUNBOOK.md), [TEAMS_SETUP.md](TEAMS_SETUP.md), [GCHAT_SETUP.md](GCHAT_SETUP.md) | Connecting Teams and Google Chat |
| [PROGRESS.md](PROGRESS.md), [DECISIONS.md](DECISIONS.md), [ACCEPTANCE.md](ACCEPTANCE.md), [TRACEABILITY.md](TRACEABILITY.md) | What is built, why, and how it maps to the requirements |
