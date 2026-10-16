# Decisions

Every default the build plan (`docs/development-plan-v3.md`) takes on an open question. A decision
marked **Proposed** is implemented as written but still needs a person's confirmation; **Confirmed**
means it has been agreed. To change one, edit the row here and tell the team: later phases follow
this file.

| ID | Question | Default taken | Alternative | Status |
|---|---|---|---|---|
| D1 | One channel per platform, or several? | **One per platform.** Display Name is an optional label. | Several of the same type: needs a route and an Azure Bot endpoint per bot. Out of scope until requested. | Confirmed 16 Oct 2026 |
| D2 | The Web UI has no sign-in, so no identity for permissions | The Web UI is a managed channel; chat users sign in with email and password (a chat session that cannot open the admin panel). | SSO for chat users. | Proposed |
| D2b | How does a Viewer get a password? | Admin creates the user; the panel shows a one-time set-password link (valid 24 h) to share. Same for resets. No email is sent. | Email invitations (needs the alert/email sink). | Proposed |
| D3 | SSO (Microsoft Entra ID) | Out of scope for v1; authentication sits behind an interface so SSO can be added. | Build Entra sign-in. | Proposed |
| D4 | Editors need the admin panel remotely | Local-only is the default. Remote access needs HTTPS (`ADMIN_ALLOW_REMOTE=true`, Secure cookies) and ideally a separate port so a tunnel exposes only webhooks and the chat API. | Admin on the same public address. | Confirmed 16 Oct 2026 |
| D5 | Multi-knowledge-base routing | Per channel: merge by reciprocal rank fusion (default), or choose by the asker's department. | Search one KB only. | Proposed |
| D6 | Who maintains model prices | Seed common models; Admin-editable; an unpriced call records tokens with no cost and raises an alert. | Fetch prices automatically. | Proposed |
| D7 | Export of employee conversations | Admin only, masked text, mandatory reason, audited. **Off by default** until the privacy sign-off is recorded. | Allow immediately. | Confirmed 16 Oct 2026 |
| D8 | Alert delivery | Panel banners always; optional email or Slack-webhook sink, off by default. | Panel only. | Proposed |
| D9 | Document status | Pending, Indexing, Indexed, Failed. | Without Indexing. | Proposed |
| D10 | Google Chat 25-second limit | Aim for p95 under 20 s. **No Google service account yet**, so a reply that would exceed the limit gets a polite timeout message and is logged. The late answer through the Chat API is deferred until a service account exists. | Late answer through the Chat API. | Confirmed 16 Oct 2026 (revised) |
| D11 | Scheduler | APScheduler in one process with a database job lock. v1 is single-instance. | Leader election for several instances. | Proposed |
| D12 | Database | SQLite by default; the whole test suite runs on SQLite and PostgreSQL. | PostgreSQL only. | Proposed |
| D13 | Unknown sender | Default department, role Viewer, sees only documents visible to Everyone. | Refuse unknown senders. | Proposed |
| D14 | Both models fail | Friendly error reply, Admin alert, usage row marked failed. | Queue and retry. | Proposed |
| D15 | Provider policy | An Admin approves providers in Global Settings; none approved until chosen; Grok and DeepSeek show a data-handling warning. | All providers available. | Confirmed 16 Oct 2026 |
| D16 | Redaction of legitimate contact details | Output redaction has an Admin allow-list that is never redacted. | No allow-list. | Proposed |
| D17 | Access precedence | A user needs both knowledge-base query permission and document visibility; group membership and user status are checked at query time; deleting or replacing a document removes its chunks immediately. | Document access only. | Proposed |

## Deviations from the plan, recorded as they happen

| Phase | Plan said | What was done | Why |
|---|---|---|---|
| 0 | Global Settings holds all the BRD values | Holds only the five that code uses today (top-k, score threshold, recent turns, chunk size, chunk overlap). Each later phase adds its own columns by migration. | A setting nothing reads is a setting nobody can test. |
| 0 | `DELETE /conversations/{id}` is Admin-only until users exist | Any holder of the service token may delete. | The Streamlit page, the only client, has a delete button and holds the token; admin-only would break it. Ownership rules arrive with users (Phases 2 and 7). |
| 0 | The generic form renderer supports text, secret, choice, multi-choice, lookup, boolean, number | Added `number`; `multi-choice` and `lookup` arrive with their first screen. | Untested code is not delivered. |
| 1 | Seed common model prices (D6) | No prices are seeded. The price table exists and is edited by hand; a call to an unpriced model records tokens with no cost. | Current prices cannot be verified here, and a wrong seeded price is worse than none. |
| 1 | Fallback on 402, 429, 5xx, timeout | Also when the provider is **unreachable** (connection failures). A rejected key (401, 403) does **not** fall back but raises a "key rejected" alert. | An unreachable provider is the same situation as a timeout. Whether a rejected key should also fall back is worth deciding (it is not in the agreed trigger list). |
| 1 | Fall back "only before streaming starts" | Streamed answers are collected before use, so a failure at any point before the reply is sent can still fall back. | Chat apps only ever receive whole answers. |
| 1 | Providers include Ollama (roadmap) | Not built. A local Ollama server is reachable today through "OpenAI-compatible endpoint". | Not in the BRD's provider list, and nothing local to test against. |
| 1 | Usage rows carry the department | The department is left empty until users and departments exist (Phase 2). | No department identity yet. |
| 1 | Usage & Cost screen | Recording only; the screen is Phase 9. | Per plan. |
| 0 | Container boot test | The admin page is unreachable inside Docker (it sees the Docker network, not localhost) unless `ADMIN_ALLOW_REMOTE=true`. | The remote-admin design is Phase 2 (D4); the container is published on 127.0.0.1 only. |
| 2a | Groups | Deferred to Phase 4 (documents). | Nothing in 2a needs them. |
| 2a | Retention defaults | Global maximum 365 days; General department 90 days. | Defaults; change in Settings / Departments. |
| 2a | Older `/admins` endpoints | Kept working (deprecated). | Fewer test and script changes. |
| 2a | Separate admin port | Moved to Phase 2b. | Needs the remote-access design. |
| 2b | Teams sender email | Read from the Teams roster instead of Microsoft Graph (no admin consent). | Built to spec; needs a real tenant to verify. |

| 2b | Admin port in Docker | Admin listens on 8090, published on 127.0.0.1 only, HTTPS waived there (`ADMIN_REQUIRE_HTTPS=false`). | Needs remote mode inside Docker; exposing it to others needs an HTTPS proxy. |
| 3a | Channel binding is not mandatory | The picker may be left empty (= default KB) instead of being required. | Existing channels have no binding and must keep answering. |
| 3a | Several KBs on one channel | Stored, but only the first is searched until merging (D5) is built. | Plan says single KB first. |
| 3a | Embedding model | Chroma's built-in MiniLM is the first adapter. | Same vectors as the live index, no PyTorch, no re-index. |
| 3b | Where uploads are stored | `DOCS_DIR` (default `./data/kb_files/<kb id>/<doc id>/`), not `data/docs/<kb>`. | The CLI `ingest` scans `data/docs` recursively and would index uploads twice. |
| 3b | Scanned PDFs | Fail with a clear message; no OCR. | OCR is out of scope. |
| 3b | Indexing worker | One in-process worker thread, with restart recovery. | Single-instance v1 (D11); the scheduler is not needed for a queue. |
| 3c | Time and cost estimate | Tokens are estimated as characters/4; time uses the last measured build, else 20 chunks/s. | tiktoken needs a network download; the local embedder costs nothing. |
| 3c | Documents during a rebuild | Uploads, replace, move, delete and re-index are refused until it finishes. | Avoids the new index missing changes made mid-build. |
