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
| 4a | Group deletion | Refused while a document is shared with the group. | Otherwise a restricted document could lose its last group and be treated as open. |
| 4a | Who may pick people | Editors see the active-people directory (name and email). | Needed to choose viewers; limited to active staff. |
| 4b | Who sees what | Admins see all documents; the Editor of a KB's department sees all documents in it; everyone else only what is shared with them. | They can change access anyway; the plan does not say. |
| 4b | How the filter is built | Document ids are computed from the database and sent as `doc_id in [...]` to the store. | Chroma cannot match a person or group inside a metadata string; this also reflects group changes with no re-embedding. |
| 4b | Tests that fake the store | They need a document record; retrieval only returns passages of known documents (fail closed). | A fixture `open_document` provides one. |
| 4b | CLI `ingest` settings | It follows the active index version, not the global chunk settings. | Strategies are never mixed in one index (3c). |
| 5 | Two base prompts | Answering and conversation are separate Admin-only prompts. | The assistant already behaves differently in the two modes; the plan mentions one. |
| 5 | Which department prompt applies | The asker's department. | Matches how usage and retention are attributed. |
| 5 | Empty department prompt | Allowed: publishing an empty one removes the department's additions. | A way to switch it off without deleting history. |
| 5 | Base prompts hidden from Editors | Editors cannot read the base prompt. | Plan says Admin only; it also avoids teaching prompt-injection wording. |
| 6a | Blocked questions are not stored | A blocked turn leaves no history. | Hostile text would otherwise be fed to the model in later turns. |
| 6a | Output is checked whole | Answers are collected, checked, then returned as one piece. | Channels already return whole answers; the check needs the complete text. |
| 6a | Banned topics matching | Word matching with simple plurals, not semantic. | Semantic matching needs the ML models in part 6b. |
| 6a | presidio-anonymizer not installed | Masking replaces the detected spans itself. | One less dependency; same result. |
| 6a | Allow-list seed | Starts with emails found in the documents. | So legitimate contact details are not redacted on day one. |
| 6a | Token limit | Counted as characters / 4. | tiktoken needs a network download; the limit is a rough guard. |
| 6b | Rules instead of models | Injection and toxicity detection is rule-based for now; models are added later. | Chosen by the project owner; avoids multi-GB downloads and the LLM Guard dependency downgrades. |
| 6b | Toxicity list | Profanity, threats and plain insults; slurs left out. | Conservative default; Admins add what their workplace needs. |
| 6b | Prompt-leak detection | A hidden random marker, not text matching. | Verbatim matching false-alarms on the assistant's allowed self-description. |
| 7a | Old chat files | Imported once, masked, left in place as a backup. | The originals predate masking; the owner can delete the folder later. |
| 7a | Interrupted answers | The partial text is kept only when answer checks are off; with checks on, an unfinished answer is stored empty and marked incomplete. | Unchecked partial text could hold personal data or toxic wording. |
| 7a | Conversation owner on channels | The matched person when the sender is in the Users list, otherwise unknown. | Unknown senders have no account. |
| 7b | Retention clock | Last activity (not creation) decides when a conversation expires. | An active chat should not vanish mid-use. |
| 7b | Retention schedule | 03:30 UTC daily, on by default, switchable in Settings. | Confirmed by the project owner (retention job on by default). |
| 7b | Export identity | People appear as an HMAC reference, never an email. | Confirmed by the project owner; keeps the export less sensitive. |
| 7b | Summary as reference | The summary is passed as labelled reference text and masked again. | A summary built from user text must not become a way to inject instructions. |
| 8a | Fixed size keeps its old behaviour | Tables and code blocks are kept whole only by the new strategies. | Existing indexes must not change; switching strategy is a rebuild. |
| 8a | Semantic chunking | Written in-house on top of the embedder (sentence breaks at the 90th percentile of distance). | Avoids the `langchain-experimental` dependency. |
| 8a | Tiny sections | Folded into the section before; never across pages or slides. | Keeps heading paths accurate and slides separate. |
| 8a | Vector stores for now | Chroma and pgvector are built; other stores are configurable adapters. | Project owner's decision: Chroma and pgvector now, others via configuration. |
