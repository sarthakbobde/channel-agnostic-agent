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

## Release 2 decisions

From `docs/development-plan-v4_3-release-2 1.md` (section A5), which has the full wording and replaces v4.1. They are all **Proposed** until confirmed. "Built" says which v4.3 phase implements each one; "Groundwork" is the delivered Phase 0 of v4.1. D24, D25, D27, D28, D32, D38 and D41 to D45 belonged to the feedback feature, which v4.3 removes. Their IDs are not reused.

| ID | Question | Default taken | Built | Status |
|---|---|---|---|---|
| D18 | Release 2 scope | As in plan section A1 | n/a | Proposed |
| D19 | Answer language | Always the question's language. Low-confidence detection falls back to the conversation's last language, then the KB default | Phase 2 | Proposed |
| D20 | Embedding model | Per KB. Existing KBs stay on MiniLM until an Admin switches. **Recommended for multilingual KBs: the local multilingual e5-small** (spike below) | Phase 2 | Proposed; re-run on customer data |
| D21 | Language detection and normalization | A local detector behind an adapter: **fastText lid.176**, confidence threshold **0.5** (spike below). Clean-up only, never translation | Phase 2 | Proposed; re-run on customer data |
| D22 | Glossary | Query-time expansion of the search text, as **one combined search string** (the Phase 3 comparison below). Index-time is optional per KB, for terms marked stable. Off by default | Phase 3 | Proposed |
| D23 | Trace | `answer_id` is the assistant Message id. References only, not the rendered prompt or passage text. Passage text is fetched when viewed, only for viewers who may see the document. Retention follows the conversation | Phase 1 | Proposed |
| D26 | Suggestions | Generated in the same call, 3 asked for and 2 shown, each through the D40 checks. Off by default | Phase 4 | Proposed |
| D29 | Message catalogue | English is byte-identical to today. Other languages need Admin approval | Phase 2 | Proposed |
| D30 | Google Chat buttons | In the synchronous reply (cardsV2), with clicks on the same endpoint in both event formats. A per-channel switch shows suggestions as text instead. **Not yet verified in a real Workspace** (spike pending) | Phase 4 | Proposed; spike pending |
| D31 | Per-KB retrieval settings | Threshold, top-k and strategy can be overridden per KB (threshold: KB or global only). The threshold applies per KB before the merge. **Recommended thresholds: MiniLM 0.35, e5-small 0.75** | Groundwork (per KB); Phase 1 (settable); Phase 2 (calibration) | Proposed; re-run on customer data |
| D33 | Not found | A code-owned marker that the code replaces with the not-found text. Answers carry `found` | Groundwork; Phase 2 (catalogue) | Proposed |
| D34 | Language through the router | The router keeps the user's language. The answer step receives the original masked question, and the router's standalone meaning in brackets when it differs (a confirmed deviation from v4.3; see R2-0 below) | Groundwork | **Confirmed 2026-11-02** |
| D35 | Keyword search and languages | Words keep their combining marks. Cross-language questions skip keyword search | Groundwork (words); Phase 2 (cross-language) | Proposed |
| D36 | Masking step | Masking is its own step after the input checks, and since Phase 2 it follows only the masking switch, as v4.3 says: switching the guardrails off no longer stores personal data unmasked | Groundwork; Phase 2 (own switch) | **Confirmed 2026-11-02** |
| D37 | Interactive payloads | HMAC-signed with an expiry. A click is de-duplicated per person, and only the asker may click | Phase 4 | Proposed |
| D39 | Accepted languages | A scoped setting, default all. A language outside the list gets a polite reply, with no search and no model call | Phase 2 | Proposed |
| D40 | Suggestion validation | Not a duplicate, can be answered, passes checks; the best 2 are shown | Phase 4 | Proposed |
| D46 | Document summary | A background job writes a short summary in the document's language. Display only. **Off by default** (your decision, 2026-11-02): it sends whole document text to the AI model. Switch it on in RAG settings, or per KB in Scoped settings | Phase 2 | Confirmed off by default |
| D47 | Stored suggestions | Every candidate is stored, masked, with its outcome. Deleted with the conversation | Phase 4 | Proposed |

### Phase 4 format check on the configured models (2026-11-02)

Run with your go-ahead, against the local configuration's models. Each slot was called directly, with no fallback. The
questions were 12 on `HR-Policy.pdf` (8 English, 4 Hindi), with the 5 best passages and the real system prompt. Each
question was asked once with the suggestion block, and the first 6 also without it. This was 36 calls; no errors.

| Model | Replies with a usable block | Suggestions per reply | Block leaked into the answer | In the question's language | Extra input tokens (median) | Extra output tokens (median) | Extra time (median) |
|---|---|---|---|---|---|---|---|
| Preferred: Azure OpenAI gpt-4.1 | **12 of 12** | 3.0 | 0 | 12 of 12 | +88 (1,515 vs 1,427) | +42 (92 vs 50) | +0.43 s (1.50 vs 1.07 s) |
| Fallback: DeepSeek deepseek-flash | 7 of 12 (3 of the 12 were "not found" replies, which need none) | 1.75 | 0 | 7 of 7 | +93 | +37 | +0.35 s |

- **Decision:** keep same-call mode as the default; second-call mode is not needed for the preferred model.
- When the fallback answers, some replies have no block. The answer is then shown alone and the counter goes up, which is the designed behaviour.
- Budget for the release checklist: about +90 input tokens, +40 output tokens and +0.4 s per answer, plus the checking searches.

### Phase 3 comparison: one search string or two searches merged (2026-11-02)

The sample is the repo's `HR-Policy.pdf` (44 passages), 15 questions written by Claude with vocabulary the policy does
not use (PTO, WFH, POSH, PF, ESI, TA/DA, PMS, "boss", "attire", "quit" and others), and a 15-term glossary. It is a
stand-in, not customer data.

| Model | No glossary: recall@5 / MRR | One combined string | Two searches merged by rank (RRF) |
|---|---|---|---|
| MiniLM | 0.87 / 0.72 | **1.00 / 0.97** | 1.00 / 0.88 |
| e5-small | 0.80 / 0.63 | **1.00 / 0.92** | 0.93 / 0.74 |

**The single string wins on both models, so it is the default and the only mode built.** The RRF variant was spike code
only.

### Phase 2 spike results (2026-11-02)

**Neither sample is customer data**, because the 50 to 100 documents and question pairs (plan, section 0) have not arrived. The
detector sample is 111 messages in 13 languages written by Claude (57 short, 44 sentences, 10 mixed-language), plus 5 in
romanised Hindi. The embedding sample is the repo's `data/docs/HR-Policy.pdf` (English, 44 passages) with 20 questions
written by Claude in English, Hindi and Marathi, each labelled with the section that answers it, plus 5 unanswerable
questions. Re-run both on the customer sample before D20, D21 and D31 are confirmed. The scripts are kept with the session
notes; `tests/data/retrieval_check.csv` holds the embedding questions.

Language detectors (no PyTorch; memory is the growth of one process after loading):

| Detector | All | Short (< 20 characters) | Sentences | Mixed | Memory | Load | Per message |
|---|---|---|---|---|---|---|---|
| **fastText lid.176 (chosen)** | **91%** | **84%** | 100% | 90% | **15 MB** | 0.4 s | 0.01 ms |
| langdetect | 83% | 68% | 100% | 90% | 61 MB | 0.4 s | 2 ms (11 ms p95) |
| py3langid | 81% | 68% | 100% | 70% | 87 MB | 1.0 s | 0.07 ms |
| Lingua, high accuracy | 79% | 75% | 93% | 40% | 1,160 MB | 4.0 s | 0.3 ms |
| Lingua, low accuracy | 72% | 65% | 91% | 30% | 67 MB | 0.3 s | 0.2 ms |

- fastText's misses are short Hindi and Marathi phrases confused with each other (the same script), and one-word English.
- At the chosen threshold of 0.5, 92 of 111 messages are trusted, and 96% of those are right. The other 19 fall back as D19 says.
- At 0.7, 99% of trusted messages are right, but 30 fall back.
- **Romanised Hindi** ("meri leave kitni bachi hai") is not recognised by any detector. With fastText its confidence was 0.12 to 0.38, so it always falls back to the conversation's language or the default.
- Licence: the lid.176 model is CC BY-SA 3.0 (a release checklist item).
- fastText is archived upstream, and its Python wrapper breaks on NumPy 2, so the code calls its C++ binding directly. The adapter allows a swap; py3langid is the next best.

Embedding models (recall at 5 = the answering section is among the top 5 passages):

| Model | Recall@5 en→en | hi→en | mr→en | Score of the right passage / best wrong one (Hindi questions, median) | Top score of unanswerable questions | Query time |
|---|---|---|---|---|---|---|
| MiniLM (built in) | 1.00 | 0.20 | 0.15 | 0.36 / 0.35 | 0.16 to 0.41 | 340 ms* |
| **multilingual e5-small** | 1.00 | **1.00** | **0.85** | 0.80 / 0.79 | 0.75 to 0.82 | 6 ms |

- \*The MiniLM figure includes per-call overhead in Chroma's embedding function.
- Index size: both models make 384-dimension vectors (132 KB for 44 passages). Indexing time was about 5 s for both.
- **Threshold.** e5's scores are compressed: the lowest right passage scored 0.751, and unanswerable questions scored 0.75 to 0.82. So no threshold separates them. The recommended **0.75** keeps every right passage seen here, and unanswerable questions are left to the model's not-found reply (D33). MiniLM keeps 0.35.
- No API embedding model was compared, because none has keys here and none was used without asking.
- **Targets for the retrieval check:** recall at 5 of at least 0.9 for en→en and hi→en, and 0.8 for mr→en. The test passes with e5.

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
| 8b | Local multilingual model | e5-small quantized ONNX (129 MB) through onnxruntime instead of PyTorch. | Same reasoning as for guardrails: small, no PyTorch. Downloaded with the owner's agreement. |
| 8b | Embedding secrets | Stored encrypted per knowledge base (OpenAI instead reuses the LLM Providers key). | One key per knowledge base is simple; rotating it applies to all of its index versions. |
| 8b | Embedding prices | A small built-in table of approximate list prices for the estimate; recorded costs use only prices entered by hand. | The estimate is a guide; recorded cost must not guess. |
| 8b | API adapters | Built to each service's documented request format and tested against a fake service. | No OpenAI, Azure or Cohere keys exist here; real calls need a live test with your keys. |
| 8c | Stores built now | ChromaDB and pgvector are fully working; Azure AI Search is a configurable adapter. | Project owner's decision: Chroma and pgvector now, others via configuration. |
| 8c | pgvector driver | Plain SQL through psycopg (already installed); no `pgvector` Python package. | One less dependency; vectors are sent as text and cast. |
| 8c | Azure AI Search | Built to the documented REST API (version 2024-07-01), tested against a stand-in, flagged on the screen as not live-verified. | No Azure account exists here. |
| 8c | Azure filters | Only `doc_id` can be filtered. | That is all the access filter and clean-up need. |
| 10a | Live tests | Marked `live`, skipped by default; need real credentials. | The default run must work offline. |
| 10b | Tunnel | `-Tunnel` moves the admin to its own local port. | No admin route on the tunnelled port, rather than relying on header checks alone. |
| 10c | Rate limit | Per person per minute, in memory, default 20, one setting for the API and the chat apps. | Simple and enough for one process; a shared store is needed if the server is ever scaled out. |
| 10c | Key rotation | A command that re-encrypts all or nothing; everyone is signed out when the first key changes. | A half-rotated database would be worse than none. |
| 9a | Cost | Only hand-entered prices; unpriced calls are counted and shown. Local models cost 0. | Nothing is guessed. |
| 9b | Alert audience | An alert can be for the Admins or one person (the uploader). | Editors should not see other departments' problems. |
| 9b | Notifications | Webhook (https only) and email, off by default, NEW alerts only, minimum severity setting. | Avoids repeat noise; webhook addresses are secrets. |
| 9b | Channel health | Checked every 2 minutes; alert clears itself. | Quick enough to notice an outage. |
| 9c | Audit search | Action filter is a prefix; target column widened to 254. | Targets such as `Knowledge base / file name` exceeded 64. |
| 9c | Answered vs not-found | Counted from answers saved after this change (they record `kb_ids`). | Older answers do not say which knowledge base was used. |
| 12a | File encryption | Fernet with the master key on whole files; a marker header tells encrypted from old plain files. | Chunked AES-GCM: only needed above about 100 MB; uploads are limited to 25 MB. |
| 12a | Blob storage settings | `FILE_STORE`, `AZURE_BLOB_CONNECTION_STRING`, `AZURE_BLOB_CONTAINER` in `.env`. | The admin center: the server needs the store before it can read its database settings. |
| 12a | Parsing | Every type (text files too) is decrypted into the private working folder for the parse. | Parse text files from memory: one path for all types is simpler and the file lives for milliseconds. |
| 12a | `data/docs/HR-Policy.pdf` | Still in git (several checks use it); an alert says the folder holds plain files. | Removing it needs the checks to get their own sample. |
| 12b | Token form | `<TYPE_` + first 8 hex characters of an HMAC-SHA256 keyed per KB `>`; longer on a clash. | Shorter tokens clash; a plain counter would differ between chunks indexed in a different order. |
| 12b | Keyword values | After `:`, `=`, `|` or ` - `, any value to the end of the sentence or cell; otherwise only a value starting with a number. | Masking after `is`/`of` without a number would hide ordinary sentences. |
| 12b | Custom types | Admin only; a new type is switched on at once. | Editors per department: open point in the plan. |
| 12c | Existing knowledge bases | Document masking off until an Admin switches it on (one rebuild each); new knowledge bases start with it on. | Switch it on for all at once: a rebuild of every KB at upgrade. |
| 12c | Who switches it | Admins only. | Editors of the department: turning masking off is a privacy decision. |
| 12c | Which types | The Guardrails screen's types, names, keywords, confidence and allow-list, for every knowledge base. | A list per knowledge base: more settings to keep consistent. |
| 12c | Guardrails off | No masking at indexing or search. | Mask anyway: the master switch is meant to turn every check off (12g formalises this). |
| 12c | Until reveal exists | Tokens are shown as plain labels; stored text keeps tokens. | Store labels: the values could then never be revealed. |
| 12c+ | Failure | No passage is used and an alert is raised. | Use the passages unchecked: could send personal data to an external model. |
| 12d | Who sees values | A list of groups and people per knowledge base, empty by default; Admins choose it. | Admins and the KB's Editors automatically: wider than needed. |
| 12d | Admin conversation viewer and exports | Always labels. | Reveal to Admins: the list is the only way to see values, and it is audited. |
| 12d | Reveal audit | One audit row per answer that revealed values (count and KBs). | One per value: noisy; none: no trace of who saw what. |
| 12g | Scope of "off" | Stops all masking for that scope (questions, answers, documents to the model), other guardrails stay on. | Keep masking questions: chosen by the project owner on 2 Nov 2026. |
| 12g | Questions with several KBs | Masked unless every KB the channel can search is off. | The question is masked before anyone knows which KB answers. |
| 8c | Store health cadence | Every 5 minutes. | Quick enough to catch an outage, light enough not to load the stores. |
| R2-0 | D36: masking follows the masking switch, not the guardrails master switch | Masking is its own step (`guardrails.mask_input`), but it still runs only when the guardrails master switch **and** masking are on, as before. | Running it with the master switch off would change what "guardrails off" means for every Admin, and would load the personal-data model in setups that switched it off. Confirm whether "off" should still mask. **Superseded in Phase 2:** masking now follows its own switch only, as v4.3 says (see D36). |
| R2-0 | Answers stream token by token when output checks are off | Every answer is now collected whole before it is handed over. | The not-found marker must never reach a person. Every caller (API, chat apps, preview) already joined the answer, so nothing visible changes. |
| R2-0 | The editable base prompt says to reply "I couldn't find this…" | It is left unchanged. A code-owned reply rule after it asks for the marker, and both are recognised. | Admins may have published their own base prompt, and the code must not rewrite it. |
| R2-0 | Sources gain `doc_language` | Not yet. `doc_id`, `kb_id`, `chunk_id` and `title` are added now. | Document languages are only known after Phase 3. |
| R2-0 | v4.3 D34: the answer step receives the original masked question; the standalone query is only for search | The original question is sent, and the router's standalone meaning is added in brackets when it differs | Built under v4.1, which asked for this. Without it, a follow-up such as "what about contractors?" reaches the answer step with no history. **Confirmed 2026-11-02:** kept. The answer step sees no conversation history, so a follow-up needs the router's reading; the trace records it |
| R2-1 | Editors open a trace "from Conversations", but that screen and its API were Admin only | Editors get a read-only view through the new `/admin/api/traces` routes, limited to their own departments. The Admin `/conversations` routes (delete, export, retention) stay Admin only. Stored source snippets are left out of the Editor's view | The default-deny role map stays meaningful. Snippets are passage text from documents the Editor may not be allowed to see. **An Editor can now read the masked questions and answers of their own departments, which they could not before. This belongs in the privacy sign-off** |
| R2-1 | Precedence is channel, KB, department, global | Built and tested, but none of the five first keys allows the channel scope, so the screen offers no channels yet | D31 names KB and global for the threshold, and top-k and strategy per KB. The first channel-scoped keys are the suggestion switches in Phase 4 |
| R2-1 | The department in the precedence chain is not defined for retrieval | For retrieval settings it is the department that owns the knowledge base, not the asker's | Search tuning belongs to the index. Everyone asking one KB gets the same behaviour. Keys about the asker can pass their own department later |
| R2-1 | One `top_k` per question | Each KB searches with its own value. When several are merged, the merged list keeps the largest | Otherwise a KB's override could be cut off by another KB's smaller value |
| R2-1 | Trace hits per KB | Hits below the threshold are recorded too, marked `above_threshold: false`, along with whether each was used. A turn that fails also gets a trace, with `complete: false` | "Not found" is the answer that most needs explaining. These are references only, and only passages the asker could see |
| R2-2 | Input checks run in step 3, before the language is known (step 5) | English checks stay in step 3. A language's own word lists, and the choice for a language without lists, run right after detection, on masked text | The language must be known to pick its lists |
| R2-2 | D21: detection runs on masked text | Questions are detected on masked text. Replies sent before masking (blocked, slow down, access turned off, failure, provider refusal, Google Chat timeout) pick their language by detecting the cleaned text in memory. Only the language code is used, and nothing is stored or logged | Otherwise every refusal would be in English. Detection is local |
| R2-2 | Which scope decides a turn's default language and confidence threshold | The first knowledge base asked: its own value, then its department's, then global. Accepted languages are checked for each knowledge base | One turn needs one language. Knowledge bases asked together usually share a department |
| R2-2 | Carry a `LanguageContext` through the turn | It travels in a context variable, like the asker's identity. Search (D35) and the prompt read it; no function signatures change | Same pattern as `identity.bind` and the trace recorder |
| R2-2 | LLM-drafted translations need Admin approval | AI drafts are Drafts and are never used. An Admin's own edit is approved by saving it, since the Admin is the approver. AI drafting never overwrites an approved entry. The English `blocked` entry stays the Blocked Reply Text on the Guardrails screen | One place to edit each text; a live language can never lose an approved text |
| R2-2 | Rejections and wrong-language answers are counted per language | Stored as guardrail events, with a new `language` column (a code only), and shown on a new Admin **Languages** screen, which also holds the message catalogue | Reuses the event table, which never holds content |
| R2-2 | Long documents are summarised by section, within a cap | When a document is longer than the cap, up to 5 consecutive parts are each cut to their share of the cap, summarised, and then combined. The cap bounds all the text read. Usage is recorded against the knowledge base's department | Covers the whole document at a known cost |
| R2-2 | A not-found answer in another language | Sources are cleared on `found`, not by matching the English sentence | The not-found text is now in the user's language |
| R2-2 | Where the detector model lives | Baked into the image at `/opt/langid` with a SHA-256 check, outside `/app/models`, so a mounted volume cannot hide it. Locally, `MODELS_DIR/lid.176.ftz`. Missing model: every question goes to the fallback and `/health` says `language: missing` | Never a download at run time |
| R2-2 | Web UI error texts move behind the catalogue | The web chat fetches the texts from a new `GET /catalog/{lang}` (chat sign-in required), cached for 5 minutes. Errors before the first answer are in English | The web chat has no language until it gets an answer |
| R2-2 | The recommended threshold is offered when switching models | Shown with the rebuild estimate, and offered as a one-click KB override once the new version is in use, not before | Setting e5's 0.75 while the MiniLM index still answers would drop every passage |
| R2-2 | Polite reply names the accepted languages | Each language is named in its own script (for example "English, हिन्दी") | The person may not read the reply's language |
| R2-2 | Spikes on the customer sample | Run on stand-in samples (see "Phase 2 spike results"). Numbers must be re-run on customer data | The sample has not arrived |
| R2-3 | Glossaries per KB or department | One glossary per scope, holding versions. Only one Draft at a time, copied from the Published version when it is started. Prompts allow several drafts | A term list is edited as a whole, so one Draft avoids merging two |
| R2-3 | Which department's terms apply | The department that owns the knowledge base, as for the other KB settings (R2-1). A department term is left out when any of its forms is already a form of a KB term | One department's terms can never reach another's knowledge bases |
| R2-3 | Term types | Acronym and synonym expand both ways. Preferred expands one way only: an alias adds the canonical term, and the canonical term adds nothing | Lets a department steer old wording to the current term without diluting searches |
| R2-3 | Matching | Whole words, using the keyword search's word pattern (combining marks kept). Case is folded unless a term is marked exact case. Simple plurals are folded ("leaves" matches "leave"). A form already in the question is not added again. Longer matches are tried first | D35 word rules. The keyword search's own plural rule turns "leaves" into "leav"; the glossary uses a corrected rule. The keyword search is unchanged |
| R2-3 | Index-time | Terms marked stable are added to a passage's embedded text when that passage uses them. The stored and displayed text is the original. `VectorStore.add` gains an optional `embed_texts`, and the built-in model is run in the app for it. In index mode, the meaning search adds only the non-stable terms at question time, and the keyword search adds every term | Keywords are matched against the stored text, which has no aliases |
| R2-3 | The indexed glossary is recorded, and a change flags a re-index | Each document records `<hash> kb@vN department@vN` of the stable terms it was embedded with. The Glossary screen counts documents indexed with other terms and points to Re-index all in KB documents. The existing re-index is used; there is no separate estimate | A per-document record survives partial re-indexing. Re-indexing with the same embedding model costs only the embedding |
| R2-3 | Publishing with conflicts | Refused (409) until the conflicts listed under the Draft are resolved | An ambiguous alias would expand unpredictably |
| R2-3 | Trace | `glossary` holds, per KB: the mode, the versions, the terms applied and the texts searched. `search_text` is the expanded meaning-search text. The versions are part of `config_snapshot_id` | Rollback shows up as a different snapshot |
| R2-3 | Audit | Records counts and version numbers, never the terms | Same rule as the other audit entries |
| R2-4 | Spikes: the format's reliability, tokens and latency on the preferred and fallback models; Google Chat card clicks | **Format: run afterwards with your go-ahead (see "Phase 4 format check" above).** The Google Chat spike needs a Workspace. Measured offline: the code-owned block adds about 91 input tokens and about 30 output tokens per answer for 3 suggestions; checking adds one local embedding batch and up to 3 searches. Second-call mode is built and chosen by a setting (default: same call) | Live calls and tenants need your go-ahead |
| R2-4 | Toggles per channel and KB | One scoped key, `suggestions_enabled` (channel, KB, department; global default off). Suggestions are on only when the channel's value **and** a KB asked have it on. The Web UI is the channel scope `web`, which has no row in the channels table | Each switch alone cannot turn suggestions on by surprise |
| R2-4 | D37 payload: answer id, action, platform user, expiry | Also the suggestion id, its position and its text. The user is a SHA-256 of the channel user (chat apps: channel, workspace and user; Web UI: the signed-in email), never the id itself. HMAC-SHA256 with a key derived from each `APP_SECRET_KEY`: the first signs, all verify (rotation). With no master key, suggestions are plain text | The text lets a click work even when the row could not be stored (task 5) |
| R2-4 | A replay changes nothing | Recorded by `clicked_at` on the stored row: a second click of the same button by the same person is ignored. If the row could not be stored, a replay cannot be detected | No nonce store, as D37 says |
| R2-4 | "Passes checks: output guardrails and masking" | A candidate that masking would change (it carries personal data) is dropped, not shown masked. The input checks also run on it. Their events appear in the guardrail report like questions' | A suggested question must be safe to ask as written |
| R2-4 | Validation searches and the trace | The checking searches run after the answer's trace is complete, so they never add hits to it. Their time is the `suggestions` stage | The trace explains the answer, not the suggestions |
| R2-4 | Reopened Web UI conversations show the same buttons | Chainlit cannot attach buttons to restored history, so the last answer's live suggestions are sent again as a short "Suggested questions" message with buttons. Older answers show theirs as text | Same effect for the person |
| R2-4 | New system messages | The catalogue gains `suggested`, `suggestion_invalid`, `suggestion_expired` and `suggestion_not_yours`. Accepted-language lists already saved stay valid (stored lists are no longer re-checked against the catalogue). The new entries show as Missing on the Languages screen and fall back to English | A later release must not silently turn "English and Hindi only" back into "every language" |
| R2-4 | Web API click | `POST /chat/action`. The click is recorded before the rate limit is checked, so a slowed-down click is used up | Small; noted for the release checklist |
| R2-4 | Slack buttons | Need Interactivity switched on in the Slack app (Socket Mode, no URL) and a reinstall; a person action (plan, section 0) | Without it Slack does not deliver the clicks |
