# Development Plan v3: Channel Agnostic Agent

**Status:** draft; D1, D4, D7, D10 and D15 confirmed 16 Oct 2026. **Date:** 2026-10-16. **Replaces:** build prompts v1 and v2.

**What changed from v2** (each item answers a review finding):

| # | Change | Why |
|---|---|---|
| 1 | Vector store interface gains `update_metadata` and `delete_by_document` | Phase 4 promises in-place permission changes without re-embedding; v2's interface could not do it |
| 2 | **Global Settings** table and screen built in Phase 0 | BRD and v2 refer to "Admin maximum", top-k, threshold, summarise-after-N but nothing built them |
| 3 | SQLite **and** PostgreSQL tests from Phase 0 | v2 left Postgres to the last phase |
| 4 | Dependency pinning and ML packages in the container restored | v1 had it, v2 dropped it |
| 5 | **D1 reversed:** one channel per platform | You decided "one bot that talks to all channels"; v2 reversed it, which would change every webhook address |
| 6 | Google Chat latency handling (D10): polite timeout now, async late answer deferred until a service account exists | A synchronous reply cannot deliver a late answer; no service account today |
| 7 | Viewer password provisioning, unknown-user rules, KB-versus-document precedence, both-models-fail, redaction allow-list, provider policy | Open questions found in review, now decided |
| 8 | Phase 0 also closes the public `/docs` and `/openapi.json` routes and generalises the secrets table | Found while mapping the code |
| 9 | Human prerequisites checklist at the top | External setup (Azure Bot, Workspace, HTTPS, sideloading) has lead time and must start now |
| 10 | Team and parallelism plan, with relative sizes | Four people, three BRD modules; shows what can run side by side |

---

## 0. How to use this plan

1. Put in `/docs`: `BRD - Channel Agnostic Agent.docx`, `Feature Roadmap` (xlsx), `BRD-vs-build.xlsx`, and this file.
2. Save **Part A** as `CLAUDE.md` in the repo root (or paste it once).
3. Run **one phase at a time** from Part B. A phase is finished only when its acceptance checks pass and its Phase Report is reviewed.
4. Everything the plan decides lives in `DECISIONS.md`. Change a default there and the phases follow it.

### Human prerequisites (start now; none are code)

These have lead time and block real-platform testing and several phases. Start them in parallel with Phase 0.

| Needed for | What a person must do | Lead-time risk |
|---|---|---|
| Teams | Create the Azure Bot (single tenant) or register it in the Teams Developer Portal; Entra app registration; **admin consent for Microsoft Graph** (sender email lookup); permission to **sideload** the Teams app | High: depends on tenant admins |
| Google Chat | A Google Workspace account and a Chat app; a project number | High |
| Teams, Google Chat | A **public HTTPS address** for the server (not a quick tunnel) | Medium |
| Slack identity | Add `users:read` and `users:read.email` scopes to the Slack app and **re-install** it (today: `app_mentions:read, chat:write, im:history, im:read`) | Low |
| Providers (Phase 1) | API keys and budget for **at least two** providers, so the fallback is on a different one. Note the current Gemini key has **no credit left** | Low |
| Phase 8 | A PostgreSQL instance and, for tests, Docker (or a local Postgres install). **Docker availability on the dev machine is not confirmed** | Medium |
| Phase 6 | A target server with enough memory for LLM Guard (several GB with PyTorch) | Medium |
| Governance | Decide the approved-provider list (D15) and privacy sign-off for conversation export (D7) | Medium |

---

# PART A: MASTER PROMPT (save as CLAUDE.md)

## 1. Role and mission

You are a senior full-stack engineer extending **Channel Agnostic Agent**: a knowledge-base assistant that answers employee questions from the organization's own documents, with file and page citations, inside Slack, Microsoft Teams, Google Chat and a Web UI. **One answering core serves every channel; each platform is a thin adapter**, so adding a channel needs no change to the core.

The work turns a working single-configuration prototype into a **configurable, multi-department product**. Admins manage channels, users, LLM providers, prompts, guardrails, knowledge bases and retention from the admin panel with **no code changes and no restarts**. Editors tailor the assistant and manage documents for their department. Every answer passes input and output guardrails. Documents are visible only to people allowed to see them.

## 2. Sources of truth (read all fully before writing code)

| File | Use it for |
|---|---|
| `BRD - Channel Agnostic Agent.docx` | **Requirements authority**: scope, roles, field-level specs, workflows, NFRs, risks |
| Feature Roadmap (xlsx) | **Implementation approach and tech stack.** Use sheets "maybe final", "features grouped", "Role Hierarchy". **Ignore `Sheet1`, `Sheet2` and every Status, Date and Owner column.** (The BRD names this file "Feature Roadmap - Rag Agent.xlsx"; use whichever exists.) |
| `BRD-vs-build.xlsx` | **Current state of the code**: 82 BRD requirements, each with what exists and what is missing. Rows are cited below as **Gap #n** (the *Sl No.* column) |
| `DECISIONS.md` | Every default taken in section 7 |

**Rules**
- BRD and roadmap differ → **the BRD wins**. The gap analysis is a judgement; **verify each "Built" claim in Phase 0** and log discrepancies in `PROGRESS.md`.
- Role names: BRD uses **Admin / Editor / Viewer**; roadmap "User" = Viewer, "Department Manager" = Editor. Editor inherits Viewer; Admin inherits all.
- Not in the BRD "In Scope" list → out of scope (section 3).
- **Extend, never rebuild, what is Built.** Keep the repository layout (`app/channels/`, `app/admin/`, flat core modules) and add new packages beside it. Keep all existing tests green (the suite currently has **55 tests** across channels, store/encryption, admin API and admin page).

## 3. Scope

### In scope
1. **Channels:** Slack (live), Microsoft Teams, Google Chat, Web UI; channel management; sender-email lookup.
2. **Admin panel and roles:** admins, users, departments, groups, roles (Admin/Editor/Viewer), role-based screens, audit log, global settings.
3. **Document permissions:** who can upload/delete; per-document visibility (everyone / selected people / selected groups); enforced at retrieval.
4. **LLM providers:** Anthropic, Google, Azure OpenAI, Grok (xAI), OpenAI, DeepSeek, plus any OpenAI-compatible endpoint; one preferred + one fallback model; usage and cost.
5. **Prompts:** locked Admin base prompt, editable department prompt, versions, preview, rollback.
6. **Guardrails:** LLM Guard input/output checks; personal-data masking (Presidio).
7. **Conversation memory:** database storage, summaries, per-department retention, CSV/JSON export.
8. **Knowledge pipeline:** chunking strategies, embedding models, vector stores (Chroma, pgvector, Azure AI Search), multiple knowledge bases bound to channels.
9. **Cross-cutting:** notifications/alerts, reports and dashboard, NFRs.

### Out of scope (do not build; no speculative hooks)
- Automatic model routing (one preferred + one fallback serve every step: router, answer, summarise).
- Translation; tool calling (tickets, HR/IT lookups).
- An "unanswered questions" report (per-KB counters of answered vs not-found answers ARE in scope).
- Long-term cross-conversation user memory; user self-service deletion of history.
- New retrieval techniques: hybrid/BM25, re-ranking, multi-query, MMR, retrieval test screen, evaluation sets, store-to-store migration beyond the versioned re-index. Retrieval stays **vector search, top-k 5, threshold 0.35** (now Global Settings). **The existing router step that rewrites a follow-up into a standalone query stays; "query rewriting is out of scope" means no new rewriting beyond it.** Merging results across knowledge bases (D5) is allowed.
- **Entra ID / SSO** (BRD is silent): keep local sign-in, with authentication behind an interface so SSO can be added later.
- Multiple channels of the same platform (D1). Revisit only on request.

## 4. Existing codebase (extend; verify in Phase 0)

| Area | What exists | Gap # |
|---|---|---|
| Answering core | `channels/core.py handle()` → `chat_service.chat()`; platform-neutral `Envelope`; router `chat_service._decide`; prompt hard-coded in `app/rag.py`; channels already get whole answers (the API joins the stream) | 5, 62 (Built) |
| Channels | `slack.py` (Socket Mode, live in the Sysfore workspace), `teams.py` (webhook, token check, ack-then-reply, offline tests), `gchat.py` (signed token, both event formats, 25 s guard, offline tests). Adapters declare a **SPEC**; `ChannelManager.apply` (start first, stop old only on success); admin Channels screen; encrypted secrets; statuses Running/Off/Not running/Not set up; **one entry per platform keyed by type**; webhook routes mounted once per type | 1-3, 5, 6, 8, 10-12, 14, 68 |
| Web UI | `streamlit_app.py` calling `/chat` and `/conversations`; not a managed channel; no sign-in | 4 |
| Admin / auth | `app/admin/` (`users.py`, `auth.py`, `api.py`, `cli.py`, `ui.py` + `ui/`): scrypt, last-admin protection, lockout, 8 h sessions, CSRF, HttpOnly cookie, local-only (refuses proxy headers; `ADMIN_ALLOW_REMOTE` exists). `AdminUser.role` exists but only `admin` is used and nothing checks it. Screens: Channels, Audit log, Admins, Account; responsive; sidebar shows "(upcoming)" items | 15-17, 24, 64 |
| Audit | `audit_log` table; secrets never recorded (tested); latest 200, no filters | 24 |
| Secrets | Fernet `SecretStore`; `secrets` table keyed by `(channel_type, name)` (must be generalised); **the LLM key is still a plaintext `.env` line** | 30, 63 |
| LLM | one OpenAI-compatible client in `llm.py` (`langchain-openai`), cached forever, model from `.env`. **When credit ran out (HTTP 402) the bot stopped answering.** | 29, 32 |
| Memory | per-thread text files (`app/memory.py`), kept forever, last 6 turns; question and answer written only after the whole stream finishes; `GET /conversations` lists everything | 43-45, 49 |
| Knowledge | `retriever.py`, `ingest.py` call Chroma directly; one collection `docs`; Chroma's built-in MiniLM; recursive 900/150 via `.env`; files copied to a folder + `python -m app.ingest` (`--reset` wipes); PDF/MD/TXT; metadata = source, doc_id, page | 50-61 |
| DB | SQLAlchemy + SQLite; tables created directly with a schema-version row; **no migrations** | 79 |
| Ops | `start.ps1` manual (`-Tunnel`, `-Ingest`); Dockerfile and compose exist, untested, lack the admin DB and master key | 69 |
| **Security hole** | `/chat`, `/conversations`, `DELETE /conversations/{id}` unauthenticated; **also `/docs` and `/openapi.json` are public**; all are reachable through a tunnel | **65 (URGENT)** |

## 5. Target architecture

Every channel hands its message to **one entry point** (`chat_service`). The core reads prompts, guardrail settings, model choice and global settings from the **database the admin panel writes to**.

**Question workflow (final shape)**
1. Resolve sender → email → user → department (unknown user: default department, role Viewer; see D13).
2. **Input guardrails:** prompt injection, toxicity, department banned topics, max length. Failure → Admin-set **Blocked Reply Text**, **no LLM call**.
3. **Mask personal data** before the question reaches the LLM or storage.
4. **Save the question first**; load summary + most recent turns.
5. **Router LLM call:** needs search? (greetings and chat-about-chat skip retrieval but still pass the output check) → standalone query.
6. **Retrieve** from the bound KB(s) with the **asker's access filter inside the search**. Blocked content never reaches the LLM.
7. Assemble: **base prompt → department prompt → "base rules take priority" line → context**.
8. Call the preferred model; on 402/429/5xx/timeout switch to the fallback **before any reply is sent**; never retry an invalid request. If both fail: friendly error reply, Admin alert, usage row marked failed (D14).
9. **Output guardrails** on the complete answer (toxicity, banned topics, redaction with allow-list).
10. Save the answer (marked incomplete if cut off); record usage (tokens, cost, department, KB).
11. After the reply is sent: summarise older turns if the conversation is long.

**Contract:** input `channel, workspace, room, thread, user, message_text, kb_ids`; output `answer_text, sources[] (file, page, heading, score, snippet), searched, friendly error reply` (never raise raw errors to a channel).

**Adapter pattern (central rule).** Channels, LLM providers, embedding models and vector stores each use: abstract base + one class per implementation + registry + a declarative **SPEC** field list (name, label, type, required, secret, help). Admin screens are **generated from the SPEC** (the Channels screen already does this; generalise that renderer). Each adapter has `validate()` and `test()` called **before saving**; nothing is stored if it fails. Secret fields are write-only: encrypted at rest, never returned by any API, never shown again, never in logs, audit rows or errors.

**Vector store interface (complete list):** `add`, `search(filter)`, `delete_by_document`, `delete`, **`update_metadata(ids or document, patch)`**, `count`, `health`, plus `stats` (size, last write). **One filter format**, translated by each adapter (Chroma `where`; SQL `WHERE`; OData). Metadata filtering and in-place metadata update are mandatory for every adapter.

**Stack (pin every version in `pyproject.toml`; record sizes of ML packages; install them in the Dockerfile):** FastAPI, Pydantic; admin panel HTML/CSS/vanilla JS (no framework); Streamlit; slack-bolt + slack-sdk, botbuilder-core + botframework-connector, google-auth + PyJWT; LangChain (`langchain-openai`, `langchain-anthropic`, `langchain-google-genai`, `with_fallbacks()`); llm-guard, presidio-analyzer/anonymizer, spaCy, transformers, PyTorch (check LLM Guard's maintenance status and pin a tested version); langchain-text-splitters, pypdf, python-docx, python-pptx, pdfplumber, langchain-experimental; Chroma, pgvector (langchain-postgres, psycopg), azure-search-documents; SQLAlchemy 2.x + Alembic on SQLite and PostgreSQL; cryptography (Fernet); APScheduler (single instance); tenacity; tiktoken.

## 6. Data model (every change via an Alembic migration; **existing databases upgrade in place with no data loss**)

- **Migration rules:** the live DB holds real encrypted channel secrets and admin accounts. Baseline migration = today's schema (stamp existing DBs). `admin_users` → `users` keeps every row (role Admin, default department). `secrets(channel_type, name)` is generalised to `secrets(owner_type, owner_id, name)` (owners: channel, provider, store) with data copied and ciphertext untouched.
- **New or extended:** `User` (email unique and immutable, name, role, department_id, status Active/Deactivated, password hash, last_sign_in, failed_attempts, locked_until), `SetPasswordToken` (one-time, expiring), `Department` (name, retention_days, banned_topics, is_default, editors), `Group` and `GroupMember`, `GlobalSettings` (singleton: max_retention_days, top_k, score_threshold, summarise_after_turns, recent_turns, default chunking and embedding, request timeout default, approved_providers, export_enabled, redaction_allowlist), `Channel` (+ KB bindings, optional display label), `LLMProvider`, `ModelSettings`, `ModelPrice`, `UsageRecord` (cost nullable if unpriced; status ok/failed), `Prompt`, `GuardrailSettings`, `GuardrailEvent` (no content), `KnowledgeBase`, `KBIndexVersion`, `Document` (status Pending/Indexing/Indexed/Failed), `DocumentAccess`, `KBAccess`, `Conversation`, `Message` (masked text, sources, complete flag), `Alert`, `ChatSession` (Web UI sign-in).

## 7. Decisions and defaults (record each in `DECISIONS.md`; flag for human confirmation; do not block)

| ID | Question | Default | Status |
|---|---|---|---|
| D1 | One channel per platform or several? | **One per platform** (you said one bot for all channels). Display Name is an optional label. Multiple per type would need a route and an Azure Bot endpoint per bot; out of scope until requested. | Confirmed 16 Oct 2026 |
| D2 | Web UI has no identity | Web UI is a **managed channel**; chat users sign in with email + password (a chat session that cannot open the admin panel); `/chat` takes identity from it. | Proposed |
| D2b | How do Viewers get a password? | Admin creates the user; the panel shows a **one-time set-password link** (valid 24 h) to share; same flow for resets. No email sending required. | Proposed |
| D3 | SSO | Out of scope; authentication behind an interface. | Proposed |
| D4 | Editors need the admin panel remotely | Local-only stays default. `ADMIN_ALLOW_REMOTE=true` **requires** HTTPS (Secure cookies; startup warning otherwise). Offer an `ADMIN_PORT` option so the admin runs on a **separate listener** from webhooks and `/chat`, and a tunnel exposes only the latter. | Confirmed 16 Oct 2026 |
| D5 | Multi-KB routing | Per-channel: **merge by reciprocal rank fusion** (default) or choose by the asker's department; each KB queried with its own embedder, merged by rank. | Proposed |
| D6 | Model prices | Seed common models; Admin-editable; unpriced calls record tokens with cost null and raise an alert. | Proposed |
| D7 | Export of employee chats | Admin-only; masked text; **mandatory reason**, audited. **Off by default** (`export_enabled = false` in Global Settings) until the privacy sign-off is recorded; an Admin then switches it on. | Confirmed 16 Oct 2026 (export off until sign-off) |
| D8 | Alert delivery | Panel banners always; optional `AlertSink` (SMTP and/or Slack webhook), off by default. | Proposed |
| D9 | Document status | Pending → Indexing → Indexed / Failed. | Proposed |
| D10 | Google Chat 25-second limit | Target p95 under 20 s. **No Google service account exists yet, so only the timeout path is built:** a reply that would exceed the limit gets a polite timeout message and is logged. The "working on it" + late answer through the Chat API is **deferred** until a service account is available (then add a secret field to the Google Chat SPEC). | Confirmed 16 Oct 2026 (revised: no service account yet) |
| D11 | Scheduler | APScheduler in one process with a DB job lock; scaling out later needs leader election. v1 is **single-instance**. | Proposed |
| D12 | Database | SQLite default; **tests run on SQLite and PostgreSQL from Phase 0** (CI matrix; if Docker is unavailable locally, use a Postgres install). | Proposed |
| D13 | Unknown sender | Default department, role Viewer, can see only documents visible to **Everyone**. | Proposed |
| D14 | Both models fail | Friendly error reply, Admin alert, usage row marked failed. | Proposed |
| D15 | Provider policy | Admin approves providers in Global Settings; none approved until chosen; DeepSeek and Grok show a data-handling warning. | Confirmed 16 Oct 2026 |
| D16 | Redaction vs legitimate contact details | Output redaction has an Admin **allow-list** (for example the HR mailbox) that is never redacted. | Proposed |
| D17 | Access precedence | A user needs **both** KB query permission **and** document visibility. Group membership and user status are checked **at query time**; deactivated users get nothing; deleting or replacing a document purges its chunks immediately. | Proposed |

## 8. Cross-cutting standards (every phase)

- **Security:** secrets encrypted, write-only, scrubbed. **The API checks the role on every request; the UI only hides.** One `require_permission(...)` on every admin route; one role→capability map reused by `/me/permissions`. Signed HttpOnly cookies, CSRF, lockout, 8 h. Chat API and conversation endpoints always authenticated. Built-in docs routes disabled or protected.
- **Audit:** every sign-in, failed sign-in, configuration change, prompt save/publish/rollback, document upload/delete/access change, export, retention deletion; **never secrets**. Add each audited event in the phase that creates the feature.
- **Privacy:** personal data masked before an external LLM or storage; retention enforced per department.
- **Availability:** a bad channel credential never stops a working one; hot reload for providers, prompts, guardrails, global settings and channels (rebuild objects on change; no forever-caching).
- **Performance:** guardrail models load once at startup; summarisation never blocks a reply.
- **Maintainability:** new channel/provider/embedding/store = new adapter file + registry entry only.
- **Testing:** pytest on SQLite **and** PostgreSQL. Each phase ships unit tests, API tests including **role-denial tests** (each role gets the expected 200/403), migration up/down **and upgrade-from-previous-release** tests, and fakes for external services. Real-platform tests are `@pytest.mark.live`. All earlier tests stay green.
- **Quality:** type hints, Pydantic schemas, no business logic in route handlers, structured logging with secret scrubbing, **pinned dependencies**.
- **Compatibility:** admin panel stays responsive and follows `ui-design-skill.md` (no inline script/style, no HTML strings, local-only rules, accessibility).

## 9. Process rules

1. **Plan before coding** each phase (files, migrations, routes, tests, which Gap #s it closes), then proceed without waiting unless a requirement is truly ambiguous.
2. Small commits, one feature each.
3. Keep `PROGRESS.md` (done, deviations, discrepancies with the gap analysis), `DECISIONS.md`, `ADMIN.md` (per screen, per role) and this file as `CLAUDE.md` current.
4. **Never stop at "code written":** run the tests and the app, exercise the new routes and screens, and report real results.
5. For anything needing an external system, build and test up to that boundary with fakes, then list exactly what a person must do and verify.
6. Do not invent requirements, build out-of-scope items, or silently skip in-scope ones.
7. **End every phase with a Phase Report:** Gap #s closed (with evidence), roadmap items delivered, tests added and results (both databases), migrations, new settings, manual verification steps, decisions taken, open risks. Then stop.

---

# PART B: PHASES

## Team and parallelism

Module owners are taken from the BRD scope table. Sizes are relative (S, M, L), not time estimates.

| Phase | Work | BRD module (owner per BRD) | Size | Starts after |
|---|---|---|---|---|
| 0 | Safety, foundations, global settings | All | M | none |
| 1 | LLM providers + fallback | LLM providers (Jason) | M | 0 |
| 2 | Users, departments, roles, identity | Admin panel and roles (Sarthak / Sivanesh) | L | 0 |
| 3 | Knowledge bases and documents | Knowledge pipeline (George) | L | 0 (uses stub departments until 2) |
| 4 | Document permissions | Admin + Knowledge | M | 2 and 3 |
| 5 | Prompts | Prompts and guardrails (Jason) | M | 2 |
| 6 | Guardrails | Prompts and guardrails (Jason) | L | 2 (needs 1 for blocked-reply plumbing) |
| 7 | Conversation memory | Conversation memory (Jason) | M | 2, and 6 for masking |
| 8 | Advanced knowledge pipeline | Knowledge pipeline (George) | L | 3 |
| 9 | Usage, cost, reports, alerts | Cross-cutting | M | 1, 6, 7 |
| 10 | Real-platform readiness and hardening | Channels (Sarthak / Sivanesh) | M | starts after 2; ends after 6 |
| 11 | Final acceptance | All | S | all |

**Critical path:** 0 → 2 → 4 → 6 → 7 → 11. Phases 1, 3 and 8 run beside it. Two people can run Phases 1, 2 and 3 side by side once Phase 0 lands; Phases 5 to 7 follow Phase 2.

---

## PHASE 0: Safety, foundations and global settings

**Closes:** Gap #65 (URGENT), #79, #80 (install), #69 (partly); baseline for #5, #62, #75. **Do this before any tunnel is used.**

**Tasks**
1. **Authenticate the chat API.** `/chat`, `/conversations`, `DELETE /conversations/{id}` and every sibling route require authentication. Channel webhooks stay authenticated by their platform signature; `core.handle()` is not exposed over HTTP. Interim for Streamlit until Phase 2: a service token from the environment. `DELETE` is Admin-only until users exist (owner rule arrives in Phase 7). **Disable `/docs`, `/redoc` and `/openapi.json`** unless the request is local. A test enumerates all routes and fails if any non-health, non-webhook route is unauthenticated.
2. **Characterization tests** first: threshold 0.35, top-5, `[n]` citations, the no-answer reply, router decision, last-6 memory. Verify each "Built" claim against the code; log discrepancies.
3. **Alembic:** baseline migration for the current schema; stamp existing databases; up/down tests; **upgrade-in-place test using a copy of the real current schema with data**; start scripts run `alembic upgrade head`. Generalise the `secrets` table (section 6) in the same release.
4. **Run tests on SQLite and PostgreSQL** from now on (D12).
5. **Global Settings** table and Admin screen (D-table fields in section 6), hot-reloaded; move `TOP_K`, `SCORE_THRESHOLD`, `MEMORY_MAX_TURNS`, chunk size/overlap from `.env` to it (keep `.env` as first-run seed with a deprecation warning).
6. **Dependencies and container:** pin all versions; install APScheduler (single instance, DB lock, D11), `python-docx`, `python-pptx`, `tenacity`, `tiktoken`. Fix Dockerfile and compose: admin database volume, master key, restart policy, health endpoint; build and boot it **if Docker is available** (otherwise document the gap).
7. **Generalise the SPEC form renderer** so providers, embeddings and stores reuse it unchanged (no new screens yet).
8. Create `DECISIONS.md` (D1 to D17, carrying each decision's Status from section 7; D1, D4, D7, D10 and D15 are already confirmed), `PROGRESS.md`.

**Acceptance**
- Every protected route returns 401 without credentials and 200 with; the enumeration test passes; `/docs` and `/openapi.json` are not public.
- Existing 55 tests plus characterization tests green on **both** databases; Slack behaviour unchanged.
- A copy of the live database upgrades in place with no data loss; channel secrets still decrypt.
- Global Settings changes affect the next question without restart.

---

## PHASE 1: LLM providers and fallback

**Closes:** Gap #29-33, #67, #76; part of #30, #34, #63, #81. **Directly fixes the outage when credit ran out (HTTP 402).**

**Tasks**
1. **Provider adapters** (SPEC-driven): Anthropic (`langchain-anthropic`), Google (`langchain-google-genai`), and **one OpenAI-style adapter** serving OpenAI, Azure OpenAI (endpoint, deployment, API version), Grok/xAI, DeepSeek **and a custom OpenAI-compatible endpoint** (keeps the current Gemini-compatible setup working). Fields per BRD: Provider (immutable), API Key (secret), Endpoint, Deployment/API Version (Azure), Available Models (read-only), Status Connected/Failed/Not tested.
2. **Test before saving + model list** (Admin only); nothing stored if the test fails.
3. **Provider policy (D15):** approved-providers setting; DeepSeek and Grok show a data-handling warning.
4. **Model Settings:** Preferred Model (every step: router, answer, summarise), Fallback Model (warn if same provider), Request Timeout (default from Global Settings).
5. **Fallback:** `with_fallbacks()` for 402/429/5xx/timeout only, never invalid requests, and always before a reply is sent. **Both fail → D14.** **Rebuild model objects on settings change** (replace the forever-cached `llm.py`).
6. **`.env` key migration:** on first start seed a Provider (as a custom OpenAI-compatible endpoint if that is what `.env` describes); afterwards the key lives only in the encrypted store and a warning tells the operator to delete the `.env` line.
7. **Usage recording:** `UsageRecord` + `ModelPrice` (D6); one row per LLM call (router, answer, summariser later), including failed calls.
8. **Alert framework (minimal):** `Alert` table, panel banners for "fallback used" and "provider key failing"; optional `AlertSink` (D8).
9. Audit provider and model-setting changes.

**Acceptance**
- Mocked preferred provider returning 402/429/500/timeout → answer from the fallback, banner shown; a mocked 400 is not retried; **both failing → friendly reply + alert**.
- Changing the preferred model affects the next question, no restart.
- Keys never appear in any response, log, audit row or error; a failed test stores nothing.
- Exactly one usage row per LLM call.

---

## PHASE 2: Users, departments, roles and sender identity

**Closes:** Gap #1 (email part, i.e. #13), #4, #7 (resolved by D1), #15-24, #64. **Prompts, banned topics, retention and permissions depend on this phase.**

**Tasks**
1. **Departments:** name, retention days (≤ Global Settings maximum), banned topics, editors, **Default Department** flag (exactly one).
2. **Groups and membership** (departments plus named groups) with an Admin screen.
3. **Users:** evolve `AdminUser` into `User` (section 6); roles Admin/Editor/Viewer; Users & Roles screen: add, edit, deactivate, assign role and department; set-password link flow (D2b). **No one can remove or demote themselves or the last Admin.** Migrate existing admins (role Admin, default department).
4. **RBAC:** central role→capability map from the BRD "Roles & Responsibilities" tables and the roadmap "Role Hierarchy"; `require_permission` on **every** admin route; `/me/permissions` drives role-aware screens. Editor = own department; Viewer has no panel access.
5. **Remote admin (D4):** `ADMIN_ALLOW_REMOTE` HTTPS mode and the optional `ADMIN_PORT` separate listener; document the reverse-proxy setup.
6. **Sender email lookup:** Slack `users.info` (needs the new scopes and re-install), Microsoft Graph for Teams (admin consent), Google Chat event email. Cache with TTL. `resolve_identity(envelope)` used by `chat_service`; unknown → D13.
7. **Web UI as a managed channel (D2):** email + password sign-in for chat users, `ChatSession`; `/chat` takes identity from it and replaces the interim service token. Channels get an optional display label (D1, no multi-instance).
8. Usage records now carry the resolved department; audit all user, role and department changes.

**Acceptance**
- Role-denial matrix: every admin route × every role gives the expected 200/403.
- Editor sees only their department; Viewer cannot enter the panel; last Admin cannot be removed or demoted.
- An unknown Slack user is answered under the default department and sees only "Everyone" documents; a known one maps to their own department.
- Web UI chat requires sign-in and carries the user's identity into the core; a deactivated user is refused immediately.

---

## PHASE 3: Knowledge bases and documents

**Closes:** Gap #9, #50 (settings), #51, #52 (interface + Chroma), #53-55, #57-59, #61 (batching/retries), #71, #78. **Define the metadata schema now, including allowed groups, so permissions need no re-index later.**

**Tasks**
1. **Vector store interface** exactly as listed in section 5 (**including `update_metadata` and `delete_by_document`**). Move `retriever.py` and `ingest.py` behind it with no behaviour change; Chroma is the default.
2. **Embeddings interface** (LangChain `Embeddings`): local MiniLM adapter first (replacing Chroma's built-in embedder; existing index keeps working or is rebuilt through the versioned mechanism); `test()` detects vector size.
3. **Chunker interface** with the fixed-size recursive strategy (900/150 from Global Settings, overridable per KB; overlap < size).
4. **Chunk metadata schema:** source file, page, heading path, chunk number, knowledge base, department, **allowed groups/people** (empty = everyone for now), document id.
5. **Knowledge bases:** entity + admin screen (create, edit, delete): name, description, owning department, per-KB settings inheriting Global Settings. Migrate the existing `docs` collection into a default KB without losing citations (including updating metadata on the existing chunks).
6. **Documents:** upload through the panel (replace, delete, re-index one or whole KB); types PDF, Word, Markdown, text, slides; status Pending → Indexing → Indexed / Failed (D9); background job; retry a failed document alone; move a document to another KB.
7. **Batching and retries** (tenacity, backoff on 429).
8. **Versioned index:** each KB has an active index version; changing chunking, embedding model or store builds a new version, switches over only when complete, keeps the old one for rollback; strategies never mixed in one index; **show an estimate of time and embedding cost before a rebuild; keep the last two versions** (older ones are deleted). Replace the destructive `ingest --reset`.
9. **Bind KBs to channels:** required multi-select "Knowledge Bases" in the channel SPEC; IDs travel in the `Envelope`; single KB first.
10. **Delete clean-up:** after confirmation, remove vectors, files and bindings, then the KB; audited.
11. Audit KB and document changes.

**Acceptance**
- `HR-Policy.pdf` uploaded through the panel goes Pending → Indexing → Indexed; the Slack answer still cites file and page; `.docx` and `.pptx` also index.
- Forced embedding failure → Failed with error; single-document retry works.
- Changing chunk size builds a new version and can roll back; the old index serves until the switch.
- Every stored chunk has the full metadata schema (asserted in a test); `update_metadata` changes access without re-embedding.
- Deleting a KB leaves no orphan vectors, files or bindings.

---

## PHASE 4: Document permissions and access enforcement

**Closes:** Gap #26-28, #56, #25 (document audit). **Security-critical: any leak is a release blocker.**

**Tasks**
1. **Upload and delete rights:** only Admin and Editor; Editors only within their own department's KBs; audit every upload, delete and access change.
2. **Document access** (BRD "Document Permissions"): Visible To = Everyone | Selected people | Selected groups; Managed By (uploader). Editor screen to set it per document.
3. **Access stored in every chunk** at indexing time; changes **update chunk metadata in place** through `update_metadata` (no re-embed). Groups are stored by ID; **membership and user status are resolved at query time (D17)**.
4. **Enforce at retrieval:** `resolve_identity` → groups + department → **metadata filter inside the vector search**, never after. Restricted chunks must not reach the LLM, the router, logs or the sources list. Plumb identity through `core.handle() → chat_service → retriever`.
5. **KB access:** role → KB permission (manage / upload / query); **both KB permission and document visibility are required (D17)**.
6. **Multi-KB routing (D5).**
7. **Purge:** deleting or replacing a document removes its chunks immediately.

**Acceptance (automated, adversarial)**
- A user outside the allowed group asks about content found only in a restricted document → the no-answer reply, and a spy on the LLM client proves the restricted text was never sent.
- Switching a document from Everyone to Selected groups takes effect on the next question, without re-embedding; removing a person from a group does too.
- A deactivated user, and an Editor of another department, get nothing.
- The same tests pass on every implemented store adapter.

---

## PHASE 5: Prompts

**Closes:** Gap #35-37, #25 (prompt audit).

**Tasks**
1. Move the base prompt out of `app/rag.py` into the DB (seed with the existing text). **Admin-only** edit and publish; locked from department override.
2. **Department prompts** edited by that department's Editors (and Admin); **character limit** enforced on save (limit lives in Global Settings, default 4000).
3. **Assembly (exact order):** base → department → closing line stating base rules take priority → context and question; unit-test that a department prompt containing "ignore previous instructions" cannot displace the base.
4. **Versioning, never overwrite:** each save is a new Draft; **Publish** archives the previous (one Published per scope); **Rollback** to any Archived version in one step.
5. **Preview:** Editor asks a test question; the normal pipeline runs with the draft, with no effect on users and not written to real conversation history; usage tagged as preview.
6. Audit every save, publish and rollback. Screens: Admin → Base Prompt + read-only department prompts; Editor → Department Prompt (edit, preview, publish, versions, rollback).

**Acceptance**
- A draft is invisible to users until published; rollback restores prior behaviour on the next question, no restart.
- Editor/Viewer cannot edit the base prompt (403); an Editor of X cannot touch Y.
- Over-limit prompts are rejected with a clear message.

---

## PHASE 6: Guardrails (LLM Guard + Presidio)

**Closes:** Gap #38-42, #77, #66 (masking), #70 (budget). **Measure latency and memory on the target server first.**

**Tasks**
1. **Measure before committing:** install LLM Guard + PyTorch; record memory and per-message latency on the target server in `PROGRESS.md`; define the end-to-end time budget (D10) and what degrades if exceeded.
2. **Load scanner models once at startup**; readiness on the health endpoint.
3. **Input checks** before the router: `PromptInjection` (on top of the existing prompt-level defences), `Toxicity`, `BanTopics` (department's banned topics), `TokenLimit`. Failure → Blocked Reply Text, **no LLM call**.
4. **Personal-data masking:** `Anonymize` on the question (email, phone, ID numbers, card numbers; **names excluded by default**; configurable) before the LLM and before anything is stored, **including the existing text-file memory writes until Phase 7 moves them**. `Sensitive` on the answer with redaction and the Admin **allow-list (D16)**.
5. **Output checks** on the complete answer (toxicity, banned topics, redaction); failure → Blocked Reply Text. Keep our own citation check (every `[n]` maps to a returned source). Test against `HR-Policy.pdf` that legitimate contact details survive via the allow-list.
6. **Guardrail Settings screen** (Admin): enabled, injection and toxicity on/off + thresholds, max tokens, masking, masked types, Blocked Reply Text; **Banned Topics** editable by the department's Editor.
7. `GuardrailEvent` rows (check, direction, department; never content).
8. **Fail closed:** if the guardrail layer errors, return a friendly message and raise an Admin alert.
9. Hot-reload settings from the DB.

**Acceptance**
- Injection, toxic, banned-topic and over-length inputs return the blocked reply with **zero LLM calls** (spy asserts).
- A card number, email or phone in a question never appears in the LLM request, stored memory or logs.
- A mocked toxic answer is replaced before sending; normal answers pass unchanged; the HR mailbox in an answer is not redacted.
- Latency and memory recorded; models confirmed loaded once; the time budget documented.

---

## PHASE 7: Conversation memory in the database

**Closes:** Gap #43-49, #66 (retention), #25 (export and deletion audit).

**Tasks**
1. **Conversation and Message tables**; **save the question first and the answer when it completes, even if cut off** (incomplete marked, never lost); transactional so concurrent writes cannot clash; stored text is the masked text from Phase 6.
2. **One-off idempotent migration** of the old per-thread text files; then retire the file storage in `app/memory.py`.
3. **Summarised history:** after N turns (Global Settings), a **background task after the reply is sent** summarises older turns plus the previous summary with the fallback-wrapped LLM (usage recorded); next context = summary + last few turns verbatim.
4. **Retention:** per-department days (≤ Global Settings maximum; Editor sets own); **daily APScheduler job** deletes expired conversations; each run audited with counts.
5. **Export (D7):** Admin-only CSV/JSON streaming, filtered by date and department, **mandatory reason**, audited. **Disabled by default**; an Admin enables it in Global Settings once the privacy sign-off exists.
6. **Conversation access:** `GET /conversations` returns only the caller's own to Viewers and the Web UI; Admins browse all on a Conversations screen; `DELETE` limited to owner or Admin.

**Acceptance**
- Kill the process mid-answer: question stored, answer marked incomplete, no turn lost.
- A 20-turn chat answers a follow-up that depends on turn 2 via the summary.
- Retention removes only expired conversations of the right department and audits it; export honours filters, requires a reason and audits.
- A Viewer cannot read or delete someone else's conversation.

---

## PHASE 8: Advanced knowledge pipeline

**Closes:** Gap #50 (all strategies, preview), #51 (all models), #52 (pgvector, Azure AI Search), #60, #61 (embedding cost), #72.

**Tasks**
1. **Chunking strategies** (per-KB selector): Fixed size (default), **Per page** (PDF pages, one chunk per slide, page numbers kept, long pages split), **By heading** (Markdown/HTML/Word, heading path on each chunk, tiny merged, oversized split), **Semantic** (SemanticChunker on the KB's embedding model; UI warns it is slower and costlier). **Tables and code blocks kept whole** (pdfplumber); split by rows only if huge.
2. **Chunk preview:** endpoint + screen running the chosen chunker on a sample without saving.
3. **Embedding adapters** (SPEC-driven, set at KB creation; changing = new versioned index): Local MiniLM (default), **OpenAI** (reuses the key under LLM Providers), **Azure OpenAI**, **Cohere**, **Multilingual** (local, query/passage prefixes); `test()` with vector-size detection.
4. **Embedding cost:** count tokens and write `UsageRecord(kind=embedding, kb_id)`.
5. **pgvector adapter:** vector column + HNSW index, shares the admin Postgres once off SQLite; filter → SQL `WHERE`; `update_metadata` via SQL.
6. **Azure AI Search adapter:** endpoint, key, index name; creates the index if missing; OData filters; `update_metadata` via merge.
7. **Store settings and secrets** from each SPEC (keys encrypted); `test()` checks reachability, login, index exists (or creates it).
8. **Store health and stats:** count, size, last indexed; APScheduler ping flags an unreachable store (status + alert).
9. One **contract test suite** run against all three stores (filter correctness, access filters, `update_metadata`, delete).

**Acceptance**
- Contract tests pass on Chroma, pgvector (real Postgres) and Azure AI Search (fake or `live`).
- Chunk preview equals real ingestion output for the same file and settings.
- Tables stay whole; per-page and heading chunks carry correct page numbers and heading paths.
- Switching a KB's store or embedding model builds a new version with queries unaffected until switch-over.

---

## PHASE 9: Usage, cost, reports and alerts

**Closes:** Gap #34 (screen), #81, #82, #24 (filters), #73, remaining #25.

**Tasks**
1. **Usage & Cost** screen (Admin all; Editor own department): tokens and cost by provider, model, department, KB, by day and month, with the price editor (D6) and an "unpriced" indicator.
2. **Alerts:** complete the BRD table: fallback used; provider key failing; channel stopped or failing; document indexing failed (uploader + Admin); vector store unreachable; prompt published or rolled back and retention completed (audit log). Optional `AlertSink` (D8).
3. **Reports:** Guardrails (blocked by check and department), Knowledge Bases (documents by status, chunks, store size, last indexed, questions answered vs not-found, tokens), Channels (status, conversations per channel).
4. **Audit log screen:** filters by user, action and date, with pagination (replaces the fixed latest 200).
5. Confirm every event in the BRD "Audit Requirements" exists.

**Acceptance**
- Reports render with seeded data and match the underlying tables.
- Each BRD alert fires in a test; audit filters work.

---

## PHASE 10: Real-platform readiness and hardening (parallel track)

**Closes:** Gap #2, #3, #11, #69, #70, #74 (re-verify), remaining #4. Starts after Phase 2; ends after Phase 6 so latency includes guardrails.

**Tasks**
1. **Teams:** `/channels/teams/messages`, Bot Framework token check, ack-then-proactive-reply, Graph lookup, `test()`, Allowed Groups = tenants. Runbook for the human steps in the prerequisites table.
2. **Google Chat:** `/channels/gchat/events`, signed-token verification, both event formats, the **polite-timeout path (D10)** re-tested with guardrails and fallback active (the async late-answer path is deferred until a Google service account exists). Runbook for the Workspace app.
3. **Tunnel and exposure:** verify `start.ps1 -Tunnel` exposes only authenticated routes; document TLS and the reverse proxy for production; check the separate admin listener (D4).
4. **Hardening:** rate limiting on `/chat`, master-key backup procedure (and a tested key rotation, including a bulk re-encrypt command), container restart policy, concurrency and load test, **full suite on SQLite and PostgreSQL**.
5. Recorded fixtures for Teams and Google Chat events so tests are repeatable; live tests marked `@pytest.mark.live`.

**Acceptance**
- Teams and Google Chat pass offline signature and format tests; the runbook says exactly what needs a real tenant or workspace and who must do it.
- Load test shows no lost turns; the container restarts cleanly; key rotation works.

---

## PHASE 11: Final acceptance and traceability

1. `TRACEABILITY.md`: all **82 Gap #** rows plus every in-scope roadmap row mapped to code module, route, screen and test; explain any gap. Re-score the Summary table from the code.
2. End-to-end scenarios, with results:
   - **Admin:** add two providers → preferred/fallback → users and departments → KB (chunking, embedding, store) → upload → set access → bind to Slack → base prompt and guardrails → Usage & Cost.
   - **Editor (remote, HTTPS):** publish a department prompt with preview and rollback; upload a restricted document; confirm an outside Viewer cannot retrieve it.
   - **Viewer:** ask in Slack, Teams, Google Chat and the Web UI; blocked replies for injection, toxic, banned and over-length; a pasted card number masked in storage; only own conversations visible.
   - **Resilience:** preferred provider returns 402/429/5xx/timeout → fallback + banner; both fail → friendly reply + alert; one bad channel credential leaves others running; store unreachable shows status.
   - **Governance:** retention runs; export audited with a reason; the audit log has no secrets.
3. **Security pass:** privilege escalation on every admin route, secret retrieval through every API, cross-department access, permission bypass through the router, summariser and preview paths, unauthenticated routes, public docs routes.
4. List remaining human-only actions, decisions still marked Proposed, and known limitations; then stop.

---

# APPENDIX

**Role capability summary**

| Capability | Admin | Editor | Viewer |
|---|---|---|---|
| Channels, users, roles, providers, global settings, base prompt, guardrail on/off and thresholds, KB settings, retention limit, export, audit log | Yes | No | No |
| Department prompt (edit, preview, publish, rollback) | Any | Own dept | No |
| Banned topics | Any | Own dept | No |
| Upload / replace / delete / re-index documents; set document access | Any | Own dept KBs | No |
| Department retention (within the Admin maximum) | Any | Own dept | No |
| Usage and cost | All | Own dept | No |
| Ask questions | All KBs | Own dept KBs | Permitted documents only |
| Admin panel | Yes | Yes (own scope) | No |

**Status vocabularies:** Channel: Running / Off / Not running / Not set up. Provider: Connected / Failed / Not tested. Document: Pending / Indexing / Indexed / Failed. Prompt: Draft / Published / Archived. User: Active / Deactivated.

**Fallback triggers:** 402, 429, 5xx, timeout. Never invalid requests. Before any reply is sent.

**Defaults to preserve:** recursive 900/150; local MiniLM; Chroma; top-5 / 0.35 (now Global Settings); no-answer reply "I couldn't find this in the knowledge base."; last 6 turns until summarisation replaces older turns.

**Definition of done (every phase):** tests green on SQLite and PostgreSQL (including role-denial, migration up/down and upgrade-from-previous), all earlier tests still green, no secrets in any output, audit entries for every change, docs and `DECISIONS.md` updated, Phase Report delivered.
