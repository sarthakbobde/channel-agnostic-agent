# RAG Assistant: Progress Log

Date: 2026-10-12

## Goal
A simple RAG assistant that plugs into chat apps (Slack, Teams, Google Chat) through
**configuration only**. The same core answers every channel; each platform gets a thin
adapter.

## Architecture

```
Slack ──┐                        ┌─ verify request
Teams ──┼─► channel adapter ────►│─ parse → Envelope
GChat ──┘   (app/channels/)      └─ format reply
                                     │
                                     ▼
                  chat_service.chat()  → route → retrieve (Chroma) → answer (LLM)
                  memory (per conversation) · knowledge base (data/docs)
```

- **Envelope** (`app/channels/base.py`): platform-neutral message
  `{channel, workspace, room, thread, user, text}`. `conv_id = channel-workspace-room-thread`,
  so every Slack thread / Teams chat gets its own memory.
- **Adapter interface**: `start()` / `stop()` for socket-style channels, `router()` for
  webhook-style channels (Teams, Google Chat).
- **Single entry point**: `app/channels/core.py::handle()` (never raises, returns a friendly
  error reply).
- **Config**: `channels.yaml` (per-channel on/off, allow lists, `show_sources`), secrets in
  `.env` via `${VAR}`.

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
- `app/channels/{base,core,config,slack,__init__}.py`
- Slack uses **Socket Mode** (outbound websocket, so no public HTTPS URL needed).
- Behaviour: answers DMs and `@mentions`, replies in a thread, ignores bot messages,
  converts Markdown to Slack formatting, appends a *Sources* line, optional
  `allowed_channels`.

### 3. Slack app setup (done in the Sysfore workspace)
- App **RAG Assistant** created from a manifest: Socket Mode on, bot scopes
  `app_mentions:read`, `chat:write`, `im:history`, `im:read`; events `app_mention`,
  `message.im`; Messages tab enabled.
- App-level token (`connections:write`) generated, app installed, tokens stored in `.env`.
- **Verified**: a DM to the bot in Slack got a cited answer.

### 4. Local run (instead of Docker)
- Python 3.11 virtualenv at `C:\Users\sarthak.bobde\rag-venv` (kept outside the project
  because the long OneDrive path breaks pip on Windows).
- `data/docs/HR-Policy.pdf` indexed into local Chroma (41 chunks).
- LLM switched from 9Router to **Gemini** via its OpenAI-compatible endpoint (no code change,
  only `.env`): base URL `https://generativelanguage.googleapis.com/v1beta/openai/`,
  model `gemini-3.8-flash` (`gemini-2.5-flash` is retired for new accounts).

### 5. Teams adapter (built, not yet connected)
- `app/channels/teams.py`: webhook at `POST /channels/teams/messages`, Bot Framework
  authentication, immediate acknowledgement then a proactive reply, optional
  `allowed_tenants`.
- Offline tests: missing token → 401, forged token → 401, wrong content type → 415,
  `/health` → 200. **Not yet tested with a real Teams message.**
- Setup steps: see `TEAMS_SETUP.md`.

### 5b. Google Chat adapter (built, not yet connected)
- `app/channels/gchat.py`: webhook at `POST /channels/gchat/events`. Verifies Google's signed
  bearer token (`auth: project_number` or `endpoint_url`), supports both the classic Chat app
  and Workspace add-on event shapes, replies synchronously (25 s timeout guard), greets on
  *added to space*, optional `allowed_spaces`.
- Shared Markdown→chat formatting moved to `app/channels/core.py::to_mrkdwn` (used by Slack
  and Google Chat).
- Offline tests (token check stubbed): both event shapes, 401 without/with forged token,
  blocked space, timeout reply. **Not yet tested against real Google Chat.**
- Setup steps: see `GCHAT_SETUP.md`. Needs a Google Workspace account and an HTTPS URL.

### 6. Start script and project move
- `start.ps1`: `.\start.ps1` (server), `-Tunnel` (Cloudflare quick tunnel, prints the Teams
  endpoint URL), `-Ingest` (re-index `data\docs`).
- Project moved to `...\Developement\AI\rag-assistant`; server restarted from there and
  re-verified (`/health`, a cited `/chat` answer).

## How to run

```powershell
.\start.ps1            # start the server on 127.0.0.1:8000
.\start.ps1 -Ingest    # re-index data\docs first
.\start.ps1 -Tunnel    # also expose a public HTTPS URL (for Teams / Google Chat)
```

Check it: `http://127.0.0.1:8000/health` and `http://127.0.0.1:8000/docs`; or DM the bot in
Slack.

Add a channel = fill its block in `channels.yaml`, put its secrets in `.env`, set
`enabled: true`. Add documents = drop `.pdf/.md/.txt` into `data/docs` and re-run ingest.

## Key files

| File | Purpose |
|---|---|
| `app/api.py` | FastAPI app: `/chat`, `/conversations`, `/health`; mounts channel routes |
| `app/chat_service.py`, `rag.py`, `retriever.py` | Routing, retrieval, grounded answer |
| `app/channels/` | Adapters and the shared envelope / core |
| `channels.yaml` | Per-channel configuration |
| `.env` / `.env.example` | Secrets and settings (`.env` is not copied by tooling; recreate it) |
| `start.ps1` | Local start script |
| `TEAMS_SETUP.md` | Azure Bot + Teams steps |
| `streamlit_app.py` | Optional web UI (just another client of the API) |

## Open items / next steps
1. **Rotate secrets**: the Gemini key was pasted in chat; Slack tokens passed through tool
   output. Regenerate if that matters for your policy.
2. **Move `chroma_db` and `chats` out of OneDrive** (set `CHROMA_PATH` / `CHAT_DIR` in `.env`
   to a local folder, e.g. `C:\rag-data`) to avoid SQLite sync locks; re-ingest once.
3. **Teams**: create the Azure Bot (single tenant), fill `TEAMS_*` in `.env`, enable it in
   `channels.yaml`, run `.\start.ps1 -Tunnel`, set the messaging endpoint, upload the Teams
   app. Needs your admin to allow custom app sideloading.
4. **Google Chat**: adapter built; do the Cloud Console setup in `GCHAT_SETUP.md`, fill
   `GCHAT_*` in `.env`, set `gchat.enabled: true`, run `.\start.ps1 -Tunnel`.
5. **Hardening before real use**: authentication on the API, tighter CORS, rate limits,
   pinned dependency versions, a real `requirements.txt`, and a Windows service or Docker
   deployment so the server survives restarts.
6. The Docker files were updated (Slack/Teams env vars, `channels.yaml` mount) but the
   Docker path has **not** been tested.
