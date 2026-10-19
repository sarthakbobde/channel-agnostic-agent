# Channel Agnostic Agent: Progress Log

Last updated: 2026-10-16

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

### 8. Development plan v3, Phase 0: safety, foundations, global settings (2026-10-16)

Plan: `docs/development-plan-v3.md`. Decisions and deviations: `DECISIONS.md`.

**Delivered**
- **Chat API locked** (Gap #65). `/chat` and `/conversations*` need `Authorization: Bearer <CHAT_API_TOKEN>`;
  with no token configured they answer 503 (fail closed). `/docs` and `/openapi.json` are local-only. A test
  enumerates every route of the real app and fails if any answers an anonymous request with success, apart
  from `/health`. The Streamlit page reads the token from `.env` and explains a refusal.
- **Alembic migrations** (Gap #79): `0001` baseline (the schema as it was), `0002` generalise `secrets`
  to `(owner_type, owner_id, name)` for later providers and stores, `0003` `global_settings`, `0004`
  `job_locks`. A database created before migrations is stamped at the baseline and upgraded in place.
- **Global Settings:** top-k, score threshold, recent turns, chunk size and overlap now live in the
  database (seeded from `.env` on first read), editable on the new **Settings** screen, validated,
  audited, and read by retrieval, memory and indexing within seconds (2 s cache), no restart.
- **Scheduler scaffolding** (Gap #80): APScheduler plus a database job lock (decision D11); no jobs yet.
- **Dependencies pinned**; `python-docx`, `python-pptx`, `tenacity`, `tiktoken`, `alembic`, `psycopg`
  installed. **Container** fixed (admin-data volume, master key and chat token required, non-root,
  health check, restart policy) and booted once (Gap #69, partly).
- **Reusable spec-driven form** (`specForm` in the admin page) now serves Channels and Settings.
- **Tests run on SQLite and PostgreSQL** (`TEST_DB=postgres`, throwaway Docker container) and never
  touch real data (`tests/conftest.py` points everything at a temporary folder).

**Results**
- 133 tests pass on SQLite; 132 pass and 1 is skipped on PostgreSQL (the legacy-upgrade test is SQLite-only).
  21 are characterization tests locking today's answering behaviour.
- Upgrade rehearsed on a **copy** of the live database: row counts unchanged, Slack credentials decrypt with
  the real key, admins intact. The live database was then upgraded at server restart (backups in
  `%USERPROFILE%\rag-backups`). Live checks: `/chat` without token 401, wrong token 401, right token 200,
  `/docs` local 200 and through a tunnel header 403, Slack connection up.
- Container: built and booted on a throwaway project; healthy, runs as non-root, migrated its own database
  to the latest version, kept a changed setting across a restart; torn down.
- Browser check of the Settings screen (valid save persisted and audited, invalid value refused with a
  clear message, phone layout has no sideways scroll).

**Verification of the gap analysis's "Built" claims** (Phase 0 task 2)

| Claim | Result |
|---|---|
| Answering core, router, `[n]` citations, 0.35 threshold, top-5, last 6 turns (#5, #62) | Confirmed by 21 characterization tests |
| Memory in text files, kept forever, cut-off reply loses the turn (#43-45) | Confirmed (a test documents the limitation) |
| Knowledge pipeline hard-wired to one Chroma collection, PDF/MD/TXT, metadata = source, doc_id, page (#50-61) | Confirmed |
| LLM key is a plaintext `.env` line; one cached model (#30, #32) | Confirmed |
| No migrations (#79) | Confirmed, now fixed |
| Chat endpoints unauthenticated (#65) | Confirmed, now fixed |

Discrepancies found: `/docs` and `/openapi.json` were also public (not listed in the gap analysis), now
local-only; the analysis's "55 admin/auth tests" was in fact the whole suite at that time.

**Known limits after Phase 0:** `DELETE /conversations/{id}` is allowed for any holder of the service token
(the Streamlit page needs it; ownership rules arrive with users). In Docker the admin page is refused
unless `ADMIN_ALLOW_REMOTE=true` (remote-admin design is Phase 2). Docker's PostgreSQL path is covered
by tests, not by a deployed instance.

### 9. Development plan v3, Phase 1: LLM providers and fallback (2026-10-16)

**Delivered**
- **Provider adapters** (same self-describing pattern as channels): Anthropic, Google (Gemini), OpenAI,
  Azure OpenAI, Grok (xAI), DeepSeek and any OpenAI-compatible endpoint. Key tested against the provider
  before saving; stored encrypted and write-only. **Approved providers** (D15): none until an Admin chooses;
  Grok and DeepSeek show a data-handling warning.
- **Model settings:** preferred model (used for the search-or-chat decision and the answer), optional
  fallback, timeout; warns when the fallback is on the same provider.
- **Fallback:** on 402, 429, 5xx, timeout or an unreachable provider; never on a rejected request; both
  failing gives the friendly reply plus an alert. Replaces the forever-cached model: models are rebuilt from
  the database within seconds of a change, no restart. The cut-off-credit outage no longer stops the bot
  when a fallback is configured.
- **`.env` model imported once** as provider "Default (from .env)" on the first start; the key now lives
  encrypted in the database.
- **Usage records** per call (tokens, optional cost, failed attempts) and **alerts** (banners above every
  admin screen; self-clearing; dismissable).
- Migration `0005`; new **Providers** screen; the "LLM provider (upcoming)" sidebar entry is gone.

**Checks**
- 194 tests pass on SQLite (60 are for this phase: fallback triggers, never retrying a rejected request,
  both-fail, streaming, usage and cost, the store rules, hot reload, the `.env` seed, adapters with mocked
  HTTP, the admin API, and secrets never appearing in responses, the database or the audit log).
- Live on your server: the database upgraded to 0005; the `.env` model became an encrypted provider; a
  question hit the exhausted Gemini credit (HTTP 402) and the panel showed "credit exhausted... Add credit
  or use another provider"; Test connection returned Connected with 63 models; no key in any response.
- **Not exercised with a real second provider:** only fakes were used for the fallback, because the Gemini
  key has no credit and no second key exists. Add a second provider and set it as the fallback to see it live.

**Deviations** are listed in `DECISIONS.md` (no seeded prices; fallback also on unreachable; rejected keys
alert but do not fall back; Ollama only through the compatible-endpoint option; department empty in usage).

## 10. Phase 2a: users, roles and departments

* `admin_users` became `users` (name, role admin/editor/viewer, department, status, last sign-in). Migration 0006 keeps
  every existing admin and puts them in the default department **General** (retention 90 days).
* **Roles** (`app/admin/roles.py`): Admin can do everything; Editor sees only Departments (the ones they edit) and
  Account; Viewer has no panel. Permission is checked on the server by URL prefix and is **default-deny**: a new
  admin endpoint is Admin-only until it is listed on purpose.
* **Users screen**: add a person, change role/department, deactivate/activate, issue a new one-time link.
  The last active Admin cannot be demoted, deactivated or removed, and nobody can change their own role or status.
* **Set-password links**: single use, 24 hours, only a hash is stored; shown once as `/admin#setpw=<token>` (nothing is emailed).
* **Departments screen**: retention (1 to the global maximum, default 365), banned topics (one per line), editors.
  Editors change only banned topics and retention of their own departments.
* Remote admin (`ADMIN_ALLOW_REMOTE=true`) now also requires HTTPS.
* Not yet: matching chat senders to people by email (Phase 2b), groups (Phase 4), separate admin port (2b).

### Phase 2b, step 1: who is asking

* `app/identity.py` turns the sender's email into a person: known and Active -> their department; not in the list ->
  default department, Viewer (D13); Deactivated -> a polite refusal and no model call. A lookup failure falls back to unknown.
* Adapters supply `Envelope.email`: Slack via `users.info` (scopes `users:read`, `users:read.email`, re-install needed;
  a banner alert appears if they are missing), Google Chat from the event, Teams from the roster (`TeamsInfo.get_member`; untested on a real tenant).
  Lookups are cached (10 min found, 2 min not found).
* Usage records now carry the department of the turn.
* Departments added to the live database: HR, IT, Finance, Sales, Legal, Operations (90 days each).
* Banned topics and retention are stored but not yet enforced.

### Phase 2b, step 2: Web UI sign-in

* Everyone (Viewers included) signs in to the Web UI with email + password (`app/chat_auth.py`: `/chat-auth/login`, `/me`, `/set-password`).
  The token is signed with a different key purpose from the admin cookie, so it cannot open the admin panel. It dies if the person is
  deactivated, their password changes, or the Web UI channel is switched off.
* Passwords come from a one-time link for every role: Admins/Editors open it on the admin page, Viewers on the Web UI
  (`WEB_UI_URL`, default http://localhost:8501, `?setpw=<token>`).
* Conversations are private: a signed-in person's ids are stored under `web-<user id>-`; list/read/delete only reach their own.
* `CHAT_API_TOKEN` stays as an optional machine credential (service caller, unscoped). The Streamlit app no longer uses it.
* The Web UI is a channel card ("Web UI", on/off, no credentials). Unconfigured counts as on.

### Phase 2b, step 3: separate admin port

* `ADMIN_PORT` (default 0 = off). When set, the admin page and API run on their own listener (`app/admin/server.py`,
  `ADMIN_HOST` default 127.0.0.1) inside the same process, and the main port serves **no** admin route (404). A tunnel or proxy on the main
  port therefore exposes only webhooks, `/chat`, `/chat-auth` and `/health`. Needs a single server process.
* `ADMIN_ALLOW_REMOTE=true` still requires HTTPS; `ADMIN_REQUIRE_HTTPS=false` waives that for a port reachable from this machine only.
* Docker: the admin listens on 8090, published on 127.0.0.1 only, so the admin page now works in the container.
* Not enabled in your live `.env` yet (admin stays at http://127.0.0.1:8000/admin): add `ADMIN_PORT=8090` and restart to switch.

## 11. Phase 3, part 3a: knowledge bases and the interfaces behind them

* `app/knowledge/`: `stores.py` (vector store interface: add, search with one filter format, delete_by_document, delete,
  update_metadata, replace_metadata, count, health, stats; Chroma adapter), `embeddings.py` (interface; first model is Chroma's
  built-in MiniLM, so vectors and the existing index are unchanged), `chunkers.py` (interface; recursive fixed-size, 900/150 from
  Global Settings, per-KB override), `metadata.py` (the chunk schema), `kbs.py` (records, set-up, delete clean-up).
* **Chunk metadata**: source, doc_id, page, heading_path, chunk, kb_id, department, allowed_groups, allowed_people
  (`"|1|2|"`, empty = everyone). The 41 existing chunks were completed in place on first start; citations are unchanged.
* **Knowledge bases** (table `knowledge_bases`, migration 0007): the default one ("Company knowledge") points at the original
  `docs` collection; every other one gets its own collection `kb_<id>`. Admin screen "Knowledge bases" (Editors: their departments only).
  Deleting one removes its vectors and takes it out of every channel's binding; the default cannot be deleted.
* **Channel binding**: a "Knowledge bases" picker on every chat channel and the Web UI; the ids travel in the Envelope.
  Empty = the default KB. Only the first bound KB is searched for now.
* `retriever.py` and `ingest.py` now go through the store; the CLI gained `--kb <id>`.
* Backups taken before the live upgrade: `admin_data/admin.db.bak-pre-0007-*`, `chroma_db.bak-pre-3a-*`.

### Phase 3, part 3b: documents

* **Upload from the panel** (Knowledge bases screen, per knowledge base): PDF, Word (.docx), PowerPoint (.pptx), Markdown, text; up to
  `MAX_UPLOAD_MB` (25). Checks: type, size, file signature, empty file, path tricks in names; a duplicate name asks for Replace.
  Old .doc / .ppt are refused with a hint to save as .docx / .pptx.
* **Status** Pending -> Indexing -> Indexed / Failed (error shown), table `documents` (migration 0008). Indexing runs on one background worker;
  the screen refreshes itself while anything is in progress; documents left unfinished by a restart are re-queued.
* **Actions**: Replace, Re-index one document or the whole knowledge base, Retry a failed one, Move to another knowledge base (re-indexed there),
  Delete (record, original file and vectors). All audited.
* **Batching and retries**: 64 chunks per batch, up to 4 attempts with exponential backoff (bad input is never retried).
* PowerPoint slide number is the page; Word has no pages (0). Scanned PDFs without text fail with a clear message (OCR is not built).
* Originals are kept in `DOCS_DIR` (default `./data/kb_files/<kb id>/<doc id>/`), so any document can be re-indexed.
  The document that was already indexed (HR-Policy.pdf) was recorded as Indexed and a copy of its original was kept.

### Phase 3, part 3c: versioned index

* Each knowledge base has an **active index version** (table `kb_index_versions`, migration 0009). Changing chunk settings no longer touches it:
  the screen shows "rebuild needed", an **estimate** (documents, chunks, tokens, time, cost) and a **Rebuild** button. The rebuild builds a new
  collection (`kb_<id>_v<n>`) in the background with progress and Cancel; the old index keeps answering and the switch happens only when it is complete.
  Documents cannot be changed during a rebuild (409). A failed or cancelled build leaves the active index alone, removes its half-built
  collection and (on failure) raises an admin alert.
* Single-document indexing always uses the active version's chunk settings, so strategies are never mixed in one index.
* **Rollback** to the kept version, with documents added, changed or deleted since brought up to date. The last two ready versions are kept; older ones are deleted.
* `ingest --reset` is gone; use Rebuild. Deleting a knowledge base removes every version.
* Embedding cost comes from the model (`cost_per_million_tokens`; 0 for the local model).

## 12. Phase 4, part 4a: groups and per-document access (data and screens; enforcement is part 4b)

* **Groups** (tables `groups`, `group_members`; migration 0010): named sets of people. Admins manage all; an Editor manages groups that belong to a
  department they edit and can only create groups for those. A group used by a document cannot be deleted until the document's access is changed.
* **Document access** ("Visible to"): Everyone (default), Selected groups, Selected people (`document_access`). Set at upload (stored before indexing starts,
  so there is no open window) or later with the **Access** button; the uploader is recorded as the manager. Restricted with nobody chosen is refused.
* The ids are written into every chunk (`allowed_groups`, `allowed_people`) and changes update the chunks **in place** (no re-embedding). The restriction
  survives re-index, replace, move and index rebuild. Deleting a document removes its access rows.
* Group membership and a person's status are not copied into documents; part 4b reads them when a question is asked.
* People directory endpoint (`/admin/api/directory`, active people only) for the pickers.
* Audited: upload (with access), access changes (before and after), group create/update (members added and removed)/delete.

### Phase 4, part 4b: access enforced when a question is asked

* `identity.resolve` now also reads the asker's groups and the departments they edit **at question time**; the identity travels with the question
  (`core.handle` / `/chat` bind it; the retriever reads it). Nobody bound (a script) counts as an unknown Viewer: Everyone-visible documents only.
* **Retrieval** (`app/retriever.py`): for each knowledge base the asker may query, the visible document ids are worked out from the database and passed to the
  vector search as a filter (`doc_id in [...]`), i.e. **inside** the search. Restricted passages never reach the router, the model, the sources list or logs.
  Chunks that belong to no known document are never returned. A second check on each returned passage uses the access stored in the chunk and logs an error if it ever disagrees.
* **KB permission (D17)**: optional "Who can ask" per knowledge base (groups); none = everyone on a channel bound to it. Both it and document visibility are needed.
  A group that restricts a knowledge base cannot be deleted.
* **Admins** see all documents; the **Editor of a knowledge base's department** sees all documents in it (they can change their access anyway);
  **deactivated** people are refused before any model call; **unknown senders** see Everyone-visible documents only (D13).
* **Several knowledge bases on a channel** are searched separately, each with its own filter, and merged by reciprocal rank fusion (D5).
* Delete and replace remove the old chunks immediately (tested).
* Admin screens: Upcoming list refreshed to the remaining phases (prompts, guardrails, conversations, chunking strategies, embedding models, vector stores, usage and cost, reports).

## 13. Phase 5: prompts

* **Prompts in the database** (`prompts` table, migration 0012, `app/prompts.py`): two base prompts, **answering** (from documents) and **conversation**,
  seeded from the text that used to be fixed in `app/rag.py` (the code text stays as the seed and as a last resort if the database cannot be read),
  plus one optional **department prompt** per department.
* **Assembly order**: base prompt, department prompt, a line saying the base rules take priority, then the retrieved context. With no department prompt
  the result is byte-for-byte what it was before. A department prompt that says "ignore previous instructions" cannot displace the base (tested).
  The asker's department decides which department prompt applies.
* **Versions**: every save is a new Draft, nothing is overwritten; Publish makes a Draft live and archives the previous one (one Published at a time);
  Rollback re-publishes an Archived version. A draft does nothing until published; changes apply on the next question (no cache, no restart).
* **Who**: the base prompts are Admin only (Editors get 403, even for reading). A department's prompt: an Admin or an Editor of that department.
  Department prompts are limited to `prompt_max_chars` (Settings, default 4000, range 200 to 20000); base prompts to 20000. Over-limit saves are rejected with a clear message.
* **Preview**: "Try it" runs the normal pipeline with the text in the editor; nothing is published, no conversation is written, and model calls are tagged `preview-router` / `preview-answer`.
* **Screen**: Prompts (Admins: both base prompts and every department; Editors: their departments). Audit: save, publish, rollback and preview (lengths and versions only, never the text).

## 14. Phase 6, part 6a: guardrails (rules and personal-data masking)

**Measured on this machine (Windows, 15.6 GB RAM, CPU only) with Presidio 2.2.364 + spaCy 3.8.16 + `en_core_web_sm` 3.8.0:**
the detection engine loads once in about 3 to 15 s (first import is slowest) and adds about 140 MB to the server process (169 MB total for the test
process). Checking one question takes a median of about 16 to 22 ms and a p95 of about 52 to 94 ms. Packages added: roughly 150 MB on disk (spaCy,
its dependencies, the 12 MB model). **Time budget (D10)**: the checks add under 0.1 s per message, well inside the 20 s target; the model call dominates.
PyTorch, transformers and LLM Guard are NOT installed yet (part 6b); their memory and latency will be measured before they are added.

* `app/guardrails.py` + tables `guardrail_settings` and `guardrail_events` (migration 0013). Settings screen **Guardrails** (Admin): on/off, longest question
  (tokens, about 4 characters each), blocked reply text, banned-topic enforcement, masking on/off and which types (default email, phone, card, SSN, Aadhaar, PAN,
  IBAN), mask names (off), check answers, redact personal data in answers, and an allow-list. Applies within seconds (2 s cache).
* **Input, before any model call**: length, then the asker's department banned topics, then masking. Blocked -> the Blocked Reply Text, no model call, and the turn is
  **not stored** (hostile text must not reach later turns' history). Masked text is what the router, the model, the history and the logs see.
* **Output, on the whole answer** (so answers are collected before being returned): banned topics -> blocked reply; personal data redacted except the allow-list;
  `[n]` citations that match no returned source are removed.
* **Allow-list** started with the email addresses found in your documents (first start only).
* **Fail closed**: an error in the layer returns a friendly message with no model call, records an event and raises the Admin alert `guardrail_error`.
* Events record check, direction, action, department and a count; there is no column that could hold content. Presidio's own DEBUG log (which prints words next to a match)
  is silenced.
* Health: `/health` shows `guardrails: off | ready | loading | error`; the model is loaded at start-up in the background.
* Known limits: banned topics are word matches (with simple plurals), not meaning; name detection is approximate; Aadhaar needs a valid checksum to be recognised.
* Tests set `GUARDRAILS_ENABLED=false` (they would otherwise load the language model); the switch is for first start only, afterwards the Settings screen decides.

### Phase 6, part 6b: rule-based injection, toxicity and prompt-leak checks (models later)

* `app/guardrail_rules.py`. **Injection**: weighted patterns for "ignore/disregard your instructions", asking for the system prompt, DAN / developer-mode / jailbreak,
  "pretend you have no restrictions", fake chat markers (`<|im_start|>`, `[INST]`, `system:`), and so on. A question is blocked when its score reaches the
  sensitivity threshold (low 3, medium 2, high 1); Admin phrases always block. Text is normalised first (case, look-alike letters such as Cyrillic "і",
  zero-width characters, spaced-out letters, leetspeak), so simple obfuscation does not hide a phrase.
* **Toxicity** (questions and answers): conservative built-in lists in three tiers: threats (all levels), profanity (medium and high), plain insults (high only).
  Slurs are left out on purpose; Admins add words and a "fine here" list. Obfuscated spellings (sh1t) are caught.
* **Prompt leak** (answers): a random marker is hidden in the instructions of each server process; an answer that repeats it is replaced by the blocked reply.
  No false positives from normal wording.
* Order for questions: length, injection, toxicity, banned topics, masking. Every block records an event (injection, toxicity, prompt_leak, direction, department) with no content.
* Controls on the Guardrails screen: on/off and low/medium/high for injection and toxicity, extra phrases and words, the "fine here" list, the leak check. Migration 0014.
* **Measured**: on 25 realistic normal questions (including "kill a stuck process", "heart attack leave", "system: Windows 11") there were 0 false positives at low and
  medium and 3 at high; all 12 attack styles tested were blocked with zero model calls.
* **Limits (also shown on the screen)**: rules catch known phrasing only, not paraphrases or other languages; "ignore ... instructions" in an innocent sentence
  (for example about phishing emails) is blocked. Model-based detection is a later addition (candidates reviewed: LLM Guard, whose pins would downgrade `cryptography`
  and `presidio-analyzer`; an ONNX-only route; Laya, an open ModernBERT-based model). Nothing was downloaded for this part.

## 15. Phase 7, part 7a: conversation memory in the database

* **Tables** `conversations` and `messages` (migration 0015). `app/memory.py` now reads and writes the database; the text files are no longer written.
  A conversation records its channel, owner (person, email, department), title and times; it also has `summary` and `summarised_upto` columns that part 7b uses.
* **A turn can no longer be lost**: the masked question is saved before any model runs, together with an empty answer row marked incomplete; the answer row is filled in
  and marked complete as soon as the finished answer exists (before it is handed over). If the model fails or the process is killed mid-answer, the question stays and the
  answer stays incomplete (with what had arrived, unless the answer checks were still collecting it). Tested by killing a real process mid-answer.
  A question whose answer never finished is not sent back to the model as history. Concurrent writers are safe (tested with 6 threads on one conversation).
* A **blocked question and a preview** are not stored.
* **Old files**: `chats/*.txt` are imported once at start (repeatable; an existing conversation is skipped), masked on the way in because they predate the guardrails, with the
  owner worked out from the `web-<user id>-` file name. The folder is left in place as a backup; delete it when you are satisfied (it holds the unmasked originals).
* **Access**: unchanged for the Web UI (each person sees, reads and deletes only their own; the prefix `web-<id>-` scopes it); deletions through the chat API are now audited.
  Channel conversations (Slack, Teams, Google Chat) are stored with the sender as owner when they match a person in the Users list.
* **Conversations screen** (Admin only): filter by department, channel, person (part of an email) and date, paging, open (shows masked text; audited as `conversation.view`,
  never containing message text), delete (audited).

### Phase 7, part 7b: summaries, retention and export

* **Long-chat summaries** (`app/summaries.py`): once `summarise_after_turns` (Settings, default 12) turns are unsummarised, all but the most recent `recent_turns` (6) are condensed,
  with the previous summary, by the normal fallback-wrapped model (purpose `summary`, so usage is recorded and attributed to the asker's department). It runs in a background
  thread after the answer is saved. The next question gets the summary plus the recent turns word for word; the summary is handed over as labelled reference text, never as
  instructions, and is masked again before it is stored. If no model can write it nothing changes and it is retried after a later turn. Stored on the conversation
  (`summary`, `summarised_upto`); an optimistic check stops two summaries overwriting each other.
* **Retention** (`app/retention.py`): a daily job (03:30 UTC, under the scheduler's database lock) deletes conversations whose last activity is older than their department's retention
  (never above the global maximum; conversations with no or a deleted department use the default department's). Each run, scheduled or "Run now", is audited with counts per
  department and the limits used, never any text. Settings: "Delete expired conversations every day" can switch the job off without a restart. The Conversations screen shows the last run,
  what would be deleted now per department, and a confirmed "Delete N expired now" button.
* **Export (D7)** (`POST /admin/api/conversations/export`): **off by default** (Settings: "Allow conversation export"); Admin only; a reason of at least 10 characters is mandatory; CSV or JSON,
  streamed, filtered by department and dates; the text is the stored masked text and people appear as a stable anonymous reference (an HMAC of the email), not as an email. Audited
  twice: when it starts (reason and filters) and when it ends (how many messages).
* Three new Global Settings (migration 0016): `summarise_after_turns`, `retention_job_enabled`, `export_enabled`; the Settings screen now supports on/off settings.
* Acceptance covered by the functional check: a follow-up that depends on turn 2 is answered through the summary in a 20-turn chat; retention removes only expired conversations of the
  right department and audits it; export honours its filters, needs a reason, is refused while off, and is audited.

## 16. Phase 8, part 8a: chunking strategies and chunk preview

* **Strategies** (`app/knowledge/chunkers.py`), chosen per knowledge base (new column `chunk_strategy`, also recorded on every index version; migration 0017):
  **Fixed size** (default; byte-for-byte the old behaviour, verified on `HR-Policy.pdf`: the same 41 chunks), **Per page or slide**, **By heading** (heading path on each chunk and at its start;
  tiny sections folded into the section before them, never across pages or slides; a file with no headings, such as a PDF, falls back to pages) and **Semantic** (cuts where the meaning changes,
  measured with the embedding model; the screen warns it is slower and costs more with a paid model).
* **Tables and code blocks stay whole** in the three structure-aware strategies (split by rows or lines only when more than 4 x the chunk size; a table's header row repeats in each piece).
  Tables in PDFs are found with `pdfplumber` (added, pinned): `HR-Policy.pdf` has 23 and all stay whole. Fixed size keeps the legacy behaviour and does not do this.
* **Structure-aware reading** (`parsers.load_blocks`): headings, paragraphs, tables and code for PDF, Word (heading styles), PowerPoint (slide titles are headings) and Markdown.
* **Chunk preview**: on a knowledge base, upload a sample file and see the chunks (page, heading path, size, text) for the strategy and sizes in the form, before saving them. It uses the same
  code as real indexing (tested to match what indexing stores). Nothing is saved.
* Changing the strategy makes the index "out of date" and goes through the existing rebuild (estimate, build beside the old index, switch when complete, roll back).
* Metadata `heading_path` is now filled for the heading strategy.
* Limits: heading detection in Word depends on the file using real Heading styles; PDFs have no heading information, so by heading uses their pages; semantic chunking uses the built-in local model for now
  (a per-knowledge-base embedding model comes in part 8b).

### Phase 8, part 8b: embedding models per knowledge base

* **Models** (`app/knowledge/embeddings.py`, each with a SPEC form, `validate()` and `test()` that reports the vector size): the built-in local MiniLM (default, unchanged);
  **Local multilingual** (`intfloat/multilingual-e5-small`, MIT, quantized ONNX through onnxruntime; no PyTorch; 129 MB, downloaded once into `MODELS_DIR` by an Admin on request;
  English, Hindi and Spanish questions find the right passages); **OpenAI** (uses the key of an OpenAI or OpenAI-compatible provider under LLM Providers, never copied);
  **Azure OpenAI** (endpoint, deployment, api-version, key); **Cohere** (key, model; documents and queries use the right input type). The API adapters were exercised against a fake service,
  not against the real OpenAI, Azure or Cohere, because no such keys exist here.
* **Per knowledge base**: the chosen model and its non-secret settings are saved on the knowledge base; secrets are stored encrypted per knowledge base and are write-only (never returned,
  never in an audit entry or error). Saving first **tests** the model: if the test fails nothing is stored. The index version remembers the model it was built with, and **questions are
  embedded with the active version's model**, so changing the model means a rebuild (existing estimate / build beside / switch / rollback); until the switch the old model keeps answering.
  Semantic chunking uses the knowledge base's own model.
* **Cost and usage**: every embedding call (documents and questions) is recorded as usage kind `embedding` with the knowledge base, the department, the tokens the service reported
  (or an estimate) and, when a price is entered under Providers, the cost. The rebuild estimate shows the model's approximate list price (a note says to check yours) and a data-leaves-the-company
  warning for the API models.
* Errors: a rate limit (429) or server error is retried with backoff (up to 4 attempts); a rejected key or bad model name is not retried. Keys are scrubbed from every message.
* New runtime dependencies pinned: `httpx`, `onnxruntime`, `tokenizers` (all were already installed through Chroma).

### Phase 8, part 8c: vector stores (ChromaDB, PostgreSQL + pgvector, Azure AI Search)

* **Stores** (`app/knowledge/storeadapters.py`, registry `STORE_TYPES`), chosen per knowledge base (columns `store`, `store_config`; recorded on every index version; migration 0019), with a form drawn from each
  store's SPEC, `validate()` and `test()`; secrets (the connection string, the Azure admin key) are stored encrypted per knowledge base, write-only, scrubbed from every message.
  * **ChromaDB**: built in, the default; nothing to configure.
  * **PostgreSQL + pgvector**: connection string (or, if empty, the admin database when that is PostgreSQL) and a schema. One table per index version (`caa_kb_<id>_v<n>`) with an HNSW cosine index; filters are
    SQL `WHERE` on the metadata with bound parameters; `update_metadata` is a jsonb merge. **Verified against a real PostgreSQL 16 + pgvector 0.8.7** (a throwaway Docker container, since removed).
  * **Azure AI Search**: endpoint, index name, admin key. Creates the index on first write (vector field, HNSW cosine), OData filters, `merge` for metadata updates. Built to Microsoft's documented REST API and tested
    only against an in-memory stand-in: **not verified against a live Azure service**.
* **A store change is a rebuild**: the new index is built in the new store beside the old one, used only when complete, and can be rolled back. Questions always go to the store of the ACTIVE index version. Pruning old versions,
  deleting a knowledge base and failed builds clean up in whichever store each version lives in (including the pgvector tables).
* **Store health**: a check every 5 minutes (scheduler, under the database lock) pings every knowledge base's active store; an unreachable store raises an Admin alert ("The vector store of 'X' is not answering") that clears itself
  when it answers again and never contains a connection string. The Knowledge bases screen shows reachable / not reachable, chunks, size and last indexed per knowledge base.
* **Contract tests** (`tests/test_store_contract.py`, 34 cases): the same checks run against Chroma, pgvector (when `TEST_PGVECTOR_URL` is set) and the Azure stand-in: add/search ranking, idempotent add, equality and `in`
  filters (applied inside the search, an empty allow-list matches nothing, quotes and SQL text are data), delete by document, in-place `update_metadata` (same vector, merged metadata), `replace_metadata`, count/stats/health/paging,
  delete of the whole index. The first run found one real difference (Chroma refuses an empty `in` list); it now returns nothing like the others.

## 17. Phase 9: usage, cost, reports and alerts

- **9a, usage and cost:** every model call (answers, routing, summaries, previews, embeddings) is recorded. The **Usage and cost**
  tab groups by provider, model, department, knowledge base, purpose, kind, day or month, with a date range. Cost uses only
  hand-entered prices (Admin); calls without a price are counted as *unpriced* so totals are never mistaken for the whole truth.
  Local models record cost 0. Editors see only their own departments.
- **9b, alerts:** alerts carry an audience. New: *indexing failed* (to the uploader and the Admins, clears itself when the file
  indexes or is deleted) and *channel down* (checked every 2 minutes). Together with the earlier store-unreachable, fallback
  and provider-key alerts this covers the BRD list. Optional notifications (off by default): a https webhook and/or email for
  each NEW alert (Settings, Admin only; address and password stored encrypted, never returned or audited).
- **9c, audit and reports:** the **Audit log** can be filtered by person, action (prefix), target and date, with paging
  (`GET /audit/search`). New **Reports** tab (Admin): guardrail events by check, direction, action and department; per
  knowledge base documents by status, chunks, size, last indexed, answered vs not-found and embedding tokens; per channel
  status, conversations and last activity. Chat answers now remember which knowledge bases were searched
  (`kb_ids` in the message meta); older answers are not counted in answered/not-found.
- Audit coverage checked: sign-in and failed sign-in, configuration changes, prompt save/publish/rollback, document upload,
  replace, move, delete and access changes, conversation view/export/delete, retention runs. Secrets are never logged.
- Migrations 0020 (alert audience) and 0021 (audit target length 64 to 254).

## 18. Phase 10: platform readiness and hardening

### Phase 10, part 10a: Teams and Google Chat offline
- Recorded events (`tests/fixtures/`) and `tests/test_platforms.py`: real RS256 signatures made with a throwaway key
  (valid, expired, wrong audience or issuer, forged, missing) in both Google modes; classic and add-on events; Teams
  acknowledge-then-reply, allowed tenants, mention removal; Google Chat polite timeout with guardrails on.
- Live checks marked `@pytest.mark.live` (`tests/test_live_platforms.py`), skipped by default (`-m 'not live'` in `pyproject.toml`).
- `PLATFORM_RUNBOOK.md`: what is proven offline and the human-only steps.

### Phase 10, part 10b: exposure
- `tests/test_exposure.py`: every route of the app is tried anonymously through tunnel-style requests; only `/health`
  answers. An admin session cookie is refused via a proxy; with `ADMIN_PORT` the main app has no admin route.
- `start.ps1 -Tunnel` now puts the admin on its own local port (default 8001), so the tunnel never carries admin routes.
- `DEPLOYMENT.md`: what may be public, TLS, nginx example with a route allow-list, timeouts, pre-go-live checks.

### Phase 10, part 10c: hardening
- **Rate limit:** a Global Setting, questions per person per minute (default 20, 0 = off), applied to `/chat` (429 with
  `Retry-After`) and to Slack, Teams and Google Chat (a friendly "too quickly" reply). In memory, one server process.
- **Master key:** `python -m app.secretstore fingerprint | check | rotate`. `rotate` re-encrypts every stored secret with the
  first key, all or nothing; tested with old+new keys, an undecryptable secret and the command line. Procedure in `ADMIN.md`.
- **Load and concurrency:** `tests/test_hardening.py` runs 12 people x 5 turns and 30 people in one conversation in parallel;
  no turn lost, none left incomplete. It found a real race (two threads creating the guardrail settings row at once), now fixed.
- **Restart:** a container whose server process was killed was restarted by `unless-stopped` (restart count 1), became
  healthy again and kept its volume data; a manual stop stayed stopped. Checked on the existing `caa-boot-rag` image, which
  predates this phase (rebuilding means reinstalling all packages, which I did not do without approval).
- **Both databases:** the full suite runs on SQLite and PostgreSQL (`TEST_DB=postgres`, throwaway container).
- Migration 0022 (rate limit setting).

## 19. Phase 11: final acceptance

### Phase 11, part 11a: traceability
- `TRACEABILITY.md` maps all 82 BRD requirements to code, route or screen, and test, and re-scores the summary: 75 built,
  6 partial, 1 not built (was 17 / 21 / 44).

### Phase 11, part 11b: end-to-end scenarios
- The one-off development checks of phases 2 to 9 are now part of the repository (`tests/checks/`, run by `tests/test_checks.py`,
  each in its own throwaway data folders). Bringing them back meant fixing out-of-date expectations in the scripts (Editor screens, chunks
  without a document record, conversations that moved from files to the database, a chat list that included another test's
  chat), keeping the new question limit out of their way, and lengthening a timing wait. It found no product defect.
- Three new end-to-end scenarios: `scenario_admin` (providers, people, knowledge base, Slack, prompt, a question, usage and cost,
  reports, audit), `scenario_editor_viewer` (Editor over HTTPS, restricted document, blocked replies, masking) and
  `scenario_resilience_governance` (fallback, alerts, bad channel credential, store outage, retention, audited export).
- Only the language models, the provider key test and the Slack connection are stand-ins; documents are really uploaded,
  embedded, stored and searched.

### Phase 11, part 11c: security pass
- `tests/test_security_pass.py` (23 tests): every admin route against an Editor and against no sign-in, pinned role lists,
  admin-only parts of shared routes, self-promotion, deactivated people, no GET route returns any of seven planted secrets,
  nothing secret in write responses, the audit log or the database, cross-department access, restricted text never reaching a
  router, answer, summariser or preview model call, forged and missing CSRF tokens on every state-changing route, upload names
  and types and sizes, one-time links, and login not revealing which accounts exist. It found no vulnerability.

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
| `app/db.py`, `app/secretstore.py` | Admin database models and migration runner; encryption of stored secrets |
| `app/migrations/` | Alembic migrations 0001 to 0004 |
| `app/global_settings.py` | Runtime-editable settings (top-k, threshold, history, chunking) |
| `app/security.py` | Service-token check for the chat API |
| `app/llm.py` | The model chain: preferred + fallback, built from the database, usage and alerts |
| `app/providers/` | Provider adapters (Anthropic, Google, OpenAI-style, Azure), store, `.env` seed |
| `app/alerts.py`, `app/usage.py` | Admin alerts; per-call usage records |
| `app/jobs/scheduler.py` | APScheduler scaffolding and the database job lock |
| `DECISIONS.md` | Decisions D1 to D17 with status, and deviations from the plan |
| `channels.yaml` | Legacy channel config; imported once into the admin database |
| `.env` / `.env.example` | Settings (`.env` is not committed or copied by tooling; recreate it) |
| `start.ps1` | Local start script |
| `ADMIN.md`, `TEAMS_SETUP.md`, `GCHAT_SETUP.md` | Admin guide; platform setup steps |
| `tests/` | 298 tests on SQLite / 297 on PostgreSQL: unit and integration tests, the feature checks and end-to-end scenarios in `tests/checks/` (run by `tests/test_checks.py`), the security pass, exposure and hardening tests |
| `docs/` | Architecture and one-pager PDFs and their HTML sources |

## Open items / next steps

The project is feature-complete against the plan. What remains is listed in `ACCEPTANCE.md`: actions only a person can do (model
credit, Slack scopes, Teams and Google Chat setup, HTTPS hosting, key backup and rotation), decisions still marked Proposed, and the
known limitations.
