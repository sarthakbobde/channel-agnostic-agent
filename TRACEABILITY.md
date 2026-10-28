# Traceability: BRD requirements to the build

Basis: `docs/BRD-vs-build.xlsx` (82 requirements, BRD-CAA-V1.0 draft). Status is re-scored from the code at this commit.
**Test** shows what covers the row. Files in `tests/checks/` are feature checks and end-to-end scenarios run by
`tests/test_checks.py`; `tests/test_security_pass.py` is the security pass; the other files are unit and integration tests.

## Summary (re-scored)

| BRD module | Built | Partial | Not built | Total | Built or partial |
|---|---|---|---|---|---|
| Channels | 11 | 2 | 1 | 14 | 93% |
| Admin panel and roles | 14 | 0 | 0 | 14 | 100% |
| LLM providers | 6 | 0 | 0 | 6 | 100% |
| Prompts and guardrails | 7 | 1 | 0 | 8 | 100% |
| Conversation memory | 7 | 0 | 0 | 7 | 100% |
| Knowledge pipeline | 12 | 1 | 0 | 13 | 100% |
| Cross-cutting (NFR, stack, alerts, reports) | 18 | 2 | 0 | 20 | 100% |
| **All** | **75** | **6** | **1** | **82** | **99%** |

Before the build (sheet 'Summary' in the workbook): 17 built, 21 partial, 44 not built.

## Channels

| # | Requirement | Status | Code | Route / screen | Test | Note |
|---|---|---|---|---|---|---|
| 1 | Slack connector, live | Built | app/channels/slack.py | Channels tab | tests/test_channels.py; verified live in the Sysfore workspace | Reading senders' email needs the Slack scopes users:read and users:read.email (owner action). |
| 2 | Microsoft Teams connector live | Partial | app/channels/teams.py | POST /channels/teams/messages | tests/test_platforms.py | Needs a real Azure Bot, HTTPS address and sideloading (PLATFORM_RUNBOOK.md). |
| 3 | Google Chat connector live | Partial | app/channels/gchat.py | POST /channels/gchat/events | tests/test_platforms.py | Needs a real Google Workspace app and HTTPS address (PLATFORM_RUNBOOK.md). |
| 4 | Web UI connector | Built | webchat/chat.py, webchat/backend.py, app/channels/web.py, app/chat_auth.py | POST /chat, /chat-auth/*; Channels tab (Web UI) | tests/test_webchat.py, tests/test_api_security.py, tests/test_exposure.py |  |
| 5 | One answering core for every channel | Built | app/channels/core.py (handle), app/chat_service.py | - | tests/test_hardening.py, tests/test_platforms.py |  |
| 6 | Channel type, credentials (write-only), enabled, status fields | Built | app/channels/store.py, app/channels/base.py (Field SPEC), app/secretstore.py | GET/PUT/DELETE /admin/api/channels/{typ}; Channels tab | tests/test_store.py, tests/test_admin.py |  |
| 7 | Display Name field | Not built | - | - | - | Gap: channels have no separate Display Name field (the channel type is the label). Small; not planned in any phase. |
| 8 | Allowed groups (Slack channels, Teams tenants, Google Chat spaces) | Built | allowed_tenants / allowed_spaces / allowed channels fields in each adapter SPEC | Channels tab | tests/test_platforms.py (tenants, spaces), tests/test_channels.py |  |
| 9 | Knowledge Bases lookup (required, multiple) | Built | Field kind 'kbs' (app/spec.py), app/knowledge/kbs.py (resolve) | Channels tab | tests/test_platforms.py (kb_ids reach the core) | Empty means the default knowledge base. |
| 10 | Save tests and starts the channel first; bad credential never stops a working one | Built | app/channels/manager.py (apply) | PUT /admin/api/channels/{typ} | tests/test_admin.py (start error leaves the saved channel), tests/test_channels.py |  |
| 11 | Slack outbound only; Teams and Google Chat need public HTTPS | Built | app/channels/slack.py (Socket Mode); DEPLOYMENT.md | - | tests/test_exposure.py |  |
| 12 | DMs and @mentions, threaded reply, sources under answer | Built | app/channels/slack.py, app/channels/core.py (sources_line) | - | tests/test_platforms.py (sources line), tests/test_channels.py |  |
| 13 | Look up the sender's email (Slack users.info, Graph for Teams, event email for Google Chat) | Built | app/identity.py; slack.py (users.info), teams.py (roster), gchat.py (event email) | - | tests/test_platforms.py (email reaches the core) | Teams roster lookup unverified on a real tenant; Slack needs the scopes above. |
| 14 | Test connection per channel | Built | Adapter.test() in each adapter | POST /admin/api/channels/{typ}/test | tests/test_admin.py, tests/test_platforms.py |  |

## Admin panel and roles

| # | Requirement | Status | Code | Route / screen | Test | Note |
|---|---|---|---|---|---|---|
| 15 | Manage channels, admins, users and roles from one page | Built | app/admin/*, app/admin/ui/admin.js | Admin page (all tabs) | tests/test_admin_ui.py |  |
| 16 | Admin accounts: add, remove, last-admin protection | Built | app/admin/users.py, app/admin/api.py | GET/POST /admin/api/admins, Users tab | tests/test_admin.py (management rules, last admin) |  |
| 17 | Local sign-in, lockout, 8 h sessions, CSRF, HttpOnly cookie | Built | app/admin/auth.py | POST /admin/api/auth/login, /logout, /password | tests/test_admin.py (cookie, lockout, CSRF, sessions) |  |
| 18 | Roles Admin, Editor, Viewer | Built | app/admin/roles.py, app/admin/users.py | GET /admin/api/me/permissions | tests/test_security_pass.py (route lists pinned; every route against an Editor) | Admin, Editor, Viewer; Viewers have no panel. |
| 19 | User record: name, role, department, status Active/Deactivated, last sign-in | Built | app/db.py (User), app/admin/users.py (touch_sign_in) | GET/POST/PUT /admin/api/users | tests/checks/check_2a.py, tests/checks/scenario_admin.py |  |
| 20 | Users & Roles screen: add, edit, deactivate, assign role and department | Built | app/admin/users_api.py | Users tab; POST /admin/api/users/{uid}/reset-link | tests/checks/check_2a.py |  |
| 21 | API checks role on every request; screens hidden by role | Built | app/admin/roles.py (default-deny path map), admin_session in app/admin/api.py | every /admin/api route | tests/test_security_pass.py (default deny on every admin route) |  |
| 22 | Departments (editors, retention days, banned topics, default flag) | Built | app/admin/departments_api.py, app/db.py (Department, DepartmentEditor) | Departments tab; /admin/api/departments | tests/checks/check_2a.py, tests/checks/scenario_admin.py |  |
| 23 | Map sender to department by email; default department fallback | Built | app/identity.py (resolve) | - | tests/checks/check_4b.py (unknown sender is a default-department Viewer), tests/checks/check_6a.py | Unknown sender is an unknown Viewer in the default department. |
| 24 | Audit: sign-ins, failed sign-ins, config changes, secrets never logged | Built | app/channels/store.py (record), app/admin/api.py | GET /admin/api/audit/search; Audit log tab | tests/test_admin.py (failed sign-ins audited, no secrets), tests/test_exposure.py |  |
| 25 | Audit: prompt versions, document uploads/access changes, exports, retention deletions | Built | record() calls in the admin APIs (prompt.*, document.*, conversation.export, retention.run) | Audit log tab | tests/checks/check_5.py (prompts), tests/checks/check_4a.py, tests/checks/check_3c.py (documents), tests/checks/scenario_resilience_governance.py (retention, export) | Audit coverage was reviewed in Phase 9c. |
| 26 | Document permissions (Visible To, people/groups, Managed By) | Built | app/knowledge/access.py, app/groups.py, app/admin/documents_api.py, groups_api.py | GET/PUT /admin/api/documents/{id}/access; Groups tab | tests/checks/check_4a.py, tests/checks/check_4b.py, tests/checks/scenario_editor_viewer.py |  |
| 27 | Access stored in every chunk; retrieval filters by the asker | Built | app/knowledge/stores.py + storeadapters.py (doc_id filter), app/retriever.py | - | tests/checks/check_4b.py (the model never sees restricted text), tests/test_store_contract.py, tests/test_security_pass.py | Computed at question time, so a change applies at once with no re-embedding. |
| 28 | Only Admins and Editors can upload or delete documents | Built | app/admin/roles.py, documents_api.py | POST /admin/api/kbs/{id}/documents | tests/checks/scenario_editor_viewer.py, tests/test_security_pass.py (other departments refused) |  |

## LLM providers

| # | Requirement | Status | Code | Route / screen | Test | Note |
|---|---|---|---|---|---|---|
| 29 | Providers: Anthropic, Google, Azure OpenAI, Grok, OpenAI, DeepSeek | Built | app/providers/adapters.py (OpenAI, Grok/xAI, DeepSeek, Azure OpenAI, Anthropic, Google, OpenAI-compatible) | Providers tab | tests/test_providers.py | Live keys only checked for Google (Gemini). |
| 30 | Provider record with write-only key, tested before saving | Built | app/providers/store.py | POST /admin/api/providers, /providers/test | tests/test_providers.py (key encrypted, never returned or audited) |  |
| 31 | Available models filled from the provider after a test; connection status | Built | app/providers/adapters.py (list models) | POST /admin/api/providers/{pid}/test | tests/test_providers.py |  |
| 32 | Preferred model and a fallback model; request timeout | Built | app/llm.py, app/global_settings.py | GET/PUT /admin/api/model-settings | tests/test_providers.py |  |
| 33 | Fallback on 402, 429, 5xx, timeout; no retry on invalid request | Built | app/llm.py | - | tests/test_providers.py (402/429/5xx/timeout fall back; 4xx do not) |  |
| 34 | Tokens and cost recorded per call; Usage & Cost screen | Built | app/usage.py, app/admin/usage_api.py | GET /admin/api/usage, /prices; Usage and cost tab | tests/test_providers.py (usage rows, cost only when priced) |  |

## Prompts and guardrails

| # | Requirement | Status | Code | Route / screen | Test | Note |
|---|---|---|---|---|---|---|
| 35 | Locked base prompt set by Admin | Built | app/prompts.py, app/admin/prompts_api.py | Prompts tab (base prompt: Admin only) | tests/checks/check_5.py, tests/checks/scenario_admin.py |  |
| 36 | Department prompts; assembly base + department + priority line | Built | app/prompts.py (assemble) | Prompts tab; /admin/api/prompts/* | tests/checks/check_5.py, tests/checks/scenario_editor_viewer.py |  |
| 37 | Draft / Published / Archived versions, preview with a test question, rollback | Built | app/prompts.py, prompts_api.py | POST /admin/api/prompts/drafts, /preview, /versions/{id}/publish, /rollback | tests/checks/check_5.py, tests/checks/scenario_editor_viewer.py (preview, publish, rollback) |  |
| 38 | Prompt-injection check | Partial | app/guardrail_rules.py (rules), app/guardrails.py | Guardrails tab | tests/checks/check_6b.py, tests/checks/scenario_editor_viewer.py, tests/test_platforms.py | Rule-based (known phrasing); model-based detection was deferred by decision (DECISIONS.md). |
| 39 | Toxicity, banned topics, maximum message length | Built | app/guardrails.py, app/guardrail_rules.py | Guardrails tab; banned topics per department | tests/checks/check_6a.py, tests/checks/check_6b.py, tests/checks/scenario_editor_viewer.py | Toxicity is rule-based (word lists); a detection model is a later option. |
| 40 | Personal-data masking before the LLM and storage (Presidio) | Built | app/guardrails.py (Presidio masking) | Guardrails tab | tests/checks/check_6a.py, tests/checks/scenario_editor_viewer.py |  |
| 41 | Output checks on the complete answer; blocked reply text | Built | app/guardrails.py (output redaction, citation check, canary leak check) | Guardrails tab | tests/checks/check_6a.py, tests/checks/check_6b.py |  |
| 42 | Guardrail settings screen (Admin) | Built | app/admin/guardrails_api.py | GET/PUT /admin/api/guardrails | tests/checks/check_6a.py, tests/checks/scenario_admin.py |  |

## Conversation memory

| # | Requirement | Status | Code | Route / screen | Test | Note |
|---|---|---|---|---|---|---|
| 43 | Conversations stored in a database | Built | app/memory.py, app/db.py (Conversation, Message) | GET/DELETE /conversations; Conversations tab | tests/test_hardening.py (no lost turns), tests/checks/check_7a.py |  |
| 44 | Save the question first and the answer even if cut off | Built | app/memory.py (begin_turn, finish_turn, fail_turn) | - | tests/test_baseline.py, tests/checks/check_7a.py (process killed mid-answer) |  |
| 45 | Summarise older turns after the reply | Built | app/summaries.py | - | tests/checks/check_7b.py |  |
| 46 | Retention per department, daily deletion job, audited | Built | app/retention.py, app/jobs/scheduler.py | POST /admin/api/conversations/retention/run | tests/checks/check_7b.py, tests/checks/scenario_resilience_governance.py |  |
| 47 | Admin export as CSV/JSON, audited | Built | app/admin/conversations_api.py | POST /admin/api/conversations/export | tests/checks/check_7b.py, tests/checks/scenario_resilience_governance.py (reason required, audited) | Off until the privacy sign-off; needs a reason; audited. |
| 48 | Conversation record: department, channel, summary, user | Built | app/db.py (Conversation) | Conversations tab | tests/checks/check_7a.py |  |
| 49 | Viewers see only their own conversations | Built | app/api.py (_conv prefix), app/chat_auth.py | GET /conversations | tests/checks/check_7a.py (one person cannot read or delete another's chats), tests/test_api_security.py |  |

## Knowledge pipeline

| # | Requirement | Status | Code | Route / screen | Test | Note |
|---|---|---|---|---|---|---|
| 50 | Chunking strategies: fixed, per page, by heading, semantic | Built | app/knowledge/chunkers.py | POST /admin/api/kbs/{id}/chunk-preview; Knowledge bases tab | tests/checks/check_8a.py, tests/checks/scenario_admin.py | Recursive, per page, by heading, semantic. |
| 51 | Embedding model choice | Built | app/knowledge/embeddings.py | Knowledge bases tab; POST /kbs/{id}/embedding/test | tests/checks/check_8b.py, tests/test_store_contract.py (stand-in embedder) | Local multilingual, OpenAI, Azure OpenAI, Cohere; the API ones are built to the documented format and not live-verified. |
| 52 | Vector stores: Chroma, pgvector, Azure AI Search | Partial | app/knowledge/stores.py, storeadapters.py | POST /admin/api/kbs/{id}/store/test | tests/test_store_contract.py (Chroma, pgvector, Azure stand-in) | Chroma and pgvector verified for real; Azure AI Search not live-verified (no Azure account). |
| 53 | Multiple knowledge bases with inherited global defaults | Built | app/knowledge/kbs.py | Knowledge bases tab; /admin/api/kbs | tests/checks/check_3a.py, tests/checks/check_3c.py |  |
| 54 | Rebuild into a versioned index, keep old for rollback | Built | app/knowledge/versions.py | POST /admin/api/kbs/{id}/index/rebuild, /rollback | tests/checks/check_3c.py, tests/checks/check_8b.py | Keeps the last two ready versions. |
| 55 | Delete knowledge base removes vectors, files and bindings | Built | app/knowledge/kbs.py (delete_kb) | DELETE /admin/api/kbs/{id} | tests/checks/check_3a.py |  |
| 56 | Multi-knowledge-base channels: merge or choose by department | Built | app/retriever.py, app/knowledge/kbs.py | Channels tab (Knowledge bases) | tests/checks/check_4b.py (two knowledge bases merged, each with its own access filter) | Several knowledge bases are searched together; who may ask which is set by the knowledge base's query groups. |
| 57 | Upload, replace, delete, re-index, status (Pending/Indexed/Failed) | Built | app/knowledge/documents.py | Documents in the Knowledge bases tab; /admin/api/documents/* | tests/checks/check_3b.py, tests/checks/check_3c.py, tests/checks/scenario_editor_viewer.py |  |
| 58 | File types: PDF, Word, Markdown, text, slides | Built | app/knowledge/parsers.py | - | tests/checks/check_3b.py | PDF, Word, PowerPoint, Markdown, text. |
| 59 | Chunk metadata: file, page, heading path, knowledge base, allowed groups | Built | app/knowledge/metadata.py | - | tests/checks/check_3a.py, tests/checks/check_3b.py, tests/checks/scenario_admin.py (heading path) |  |
| 60 | Tables and code kept whole | Built | app/knowledge/chunkers.py | chunk preview | tests/checks/check_8a.py | Tables and code kept whole unless very large. |
| 61 | Batched embedding with retries; embedding cost recorded | Built | app/knowledge/embeddings.py (batches, tenacity retries, usage) | Usage and cost tab | tests/checks/check_8b.py, tests/checks/check_9a.py |  |
| 62 | Router decides search vs chat and rewrites the query; cited answers (file, page) | Built | app/chat_service.py (_decide), app/rag.py | POST /chat | tests/test_baseline.py (router decision, cited answers, fixed no-answer reply) |  |

## Cross-cutting (NFR, stack, alerts, reports)

| # | Requirement | Status | Code | Route / screen | Test | Note |
|---|---|---|---|---|---|---|
| 63 | Secrets encrypted at rest, write-only, scrubbed from errors and logs | Built | app/secretstore.py, app/channels/store.py, app/providers/store.py | - | tests/test_admin.py, tests/test_providers.py, tests/test_hardening.py, tests/test_security_pass.py (no GET route returns a secret) |  |
| 64 | Admin panel local-only; HttpOnly cookie, CSRF, lockout, 8 h sessions | Built | app/admin/api.py (check_local) | - | tests/test_admin.py, tests/test_exposure.py |  |
| 65 | Chat API and conversation endpoints require authentication | Built | app/security.py | /chat, /conversations | tests/test_api_security.py, tests/test_exposure.py |  |
| 66 | Personal data masked; retention enforced per department | Built | app/guardrails.py, app/retention.py | - | tests/checks/check_6a.py (masking), tests/checks/check_7b.py (retention), tests/checks/scenario_editor_viewer.py |  |
| 67 | Fallback LLM keeps answering | Built | app/llm.py | - | tests/test_providers.py |  |
| 68 | A channel with a bad credential never stops a working one | Built | app/channels/manager.py | - | tests/test_admin.py, tests/test_channels.py |  |
| 69 | Runs as a service or container and restarts automatically | Partial | docker-compose.yml (restart: unless-stopped), Dockerfile (HEALTHCHECK) | - | checked by hand in 10c on the existing image | Not set up as a Windows service; the container route was verified on an older image. |
| 70 | Google Chat replies within 25 s | Built | app/channels/gchat.py (TIMEOUT_S = 25) | - | tests/test_platforms.py | Late (asynchronous) answers wait for a Google service account (decision D10). |
| 71 | Swap the vector store without code changes | Built | app/knowledge/storeadapters.py, kbs.store_for | Knowledge bases tab | tests/test_store_contract.py |  |
| 72 | Adapter pattern for channels, providers, embeddings, stores | Built | channels/base.py, providers/base.py, knowledge/embeddings.py, knowledge/stores.py | - | tests/test_store_contract.py, tests/test_providers.py, tests/test_channels.py |  |
| 73 | Every config, prompt, document, export and deletion audited | Built | record() calls across the admin APIs | Audit log tab | tests/checks/scenario_admin.py (every configuration step), tests/checks/scenario_resilience_governance.py |  |
| 74 | Admin panel on desktop and mobile browsers | Built | app/admin/ui/admin.css (responsive) | Admin page | checked by hand on desktop and phone widths | No automated browser test. |
| 75 | FastAPI, Pydantic, Chainlit, slack-bolt, botbuilder-core, google-auth, Fernet, SQLAlchemy + SQLite, plain-JS admin page | Built | pyproject.toml (pinned) | - | whole suite |  |
| 76 | Provider libraries (langchain-anthropic, langchain-google-genai) | Built | pyproject.toml (langchain-anthropic, langchain-google-genai, langchain-openai) | - | tests/test_providers.py |  |
| 77 | LLM Guard and Presidio | Partial | app/guardrails.py (Presidio) | - | tests/checks/check_6a.py, tests/checks/scenario_editor_viewer.py | Presidio is used. LLM Guard was rejected (unmaintained, forces older cryptography); rules replace it, see DECISIONS.md. |
| 78 | python-docx, python-pptx | Built | pyproject.toml, app/knowledge/parsers.py | - | tests/checks/check_3b.py (Word and PowerPoint) |  |
| 79 | Alembic migrations | Built | app/migrations/versions (22 migrations) | - | tests/test_migrations.py (up, down, upgrade from the previous) |  |
| 80 | APScheduler | Built | app/jobs/scheduler.py (APScheduler) | - | tests/test_jobs.py |  |
| 81 | Alerts: fallback used, provider key failing, channel failing, indexing failed, store unreachable | Built | app/alerts.py, app/alert_sinks.py, app/channels/health.py, app/knowledge/storehealth.py, app/llm.py | Alert banners; Settings (notifications); /admin/api/me/alerts | tests/checks/check_9b.py, tests/checks/scenario_resilience_governance.py, tests/test_providers.py | Fallback used, provider key failing, channel down, indexing failed, store unreachable; optional webhook and email. |
| 82 | Usage & Cost, Guardrails, Knowledge Bases, Channels reports; filterable audit log | Built | app/admin/reports_api.py, usage_api.py, GET /audit/search | Reports tab, Usage and cost tab, Audit log tab | tests/checks/check_9c.py, tests/checks/scenario_admin.py |  |
