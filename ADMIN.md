# Admin guide

The admin page lets you add, change, test, enable and delete chat channels (Slack, Teams,
Google Chat) and manage their credentials, without editing files or restarting the server.

## One-time setup

1. **Create a master key** and put it in `.env` as `APP_SECRET_KEY`. It encrypts every stored
   credential.

   ```powershell
   & "$env:USERPROFILE\rag-venv\Scripts\python.exe" -m app.secretstore generate
   ```

   **Back this key up** (for example in a password manager). Without it the stored credentials
   cannot be decrypted and have to be entered again. Never commit `.env`.
2. **Create the first admin.** The password is typed at a hidden prompt (12+ characters).
   Only addresses on `ADMIN_EMAIL_DOMAIN` (default `sysfore.com`) are accepted.

   ```powershell
   & "$env:USERPROFILE\rag-venv\Scripts\python.exe" -m app.admin.cli create-admin you@sysfore.com
   ```
3. **Start the server** (`.\start.ps1`) and open <http://127.0.0.1:8000/admin>.

On the first start with a master key, the existing `channels.yaml` + `.env` channel settings
are imported into the database once (audit entry "import"). After that the database is the
source of truth, so deleting a channel in the page is not undone by a restart.

## Using the page

| Section | What it does |
|---|---|
| **Channels** | One card per platform with its status: *Running*, *Not running* (enabled but failed to start), *Off*, *Not set up*. Select a card to edit its settings. |
| Settings form | Built from the adapter's own field list. Secrets show "(set)" and can only be replaced, never read. A blank secret keeps the stored value. |
| **Enabled** + **Save changes** | Saving checks the new settings by starting the channel first. If that fails, you get the error and nothing is saved; the working connection stays. |
| **Test connection** | Checks the entered (unsaved) values against the platform: Slack `auth.test`; Teams asks Microsoft for a token; Google Chat can only check that Google's signing certificates are reachable (Google signs requests, so there is no outbound credential to try). |
| **Delete** | Two-step: click, then confirm within 5 seconds. Stops the channel and removes its settings and secrets. |
| **Audit log** | Who did what and when. Secret values are never recorded. |
| **Admins** | Add or remove admins. You cannot remove yourself or the last admin. |
| **Account** | Your name at the top right. Profile photo, department and groups, theme and accent colour, and your password (changing it signs out every other session). |

Teams and Google Chat show the endpoint path to enter at the provider. They need a public HTTPS
address (for example `.\start.ps1 -Tunnel`); Slack uses Socket Mode and needs none.

## RAG settings screen

**RAG settings** holds values the assistant reads while it runs. Changes apply within a few seconds, with
no restart. Each field shows its allowed range and what it does; a value outside the range is refused
with a message and nothing changes. Every change is written to the audit log (old value to new value).

| Setting | Meaning | Default | Applies |
|---|---|---|---|
| Passages retrieved per question | How many of the closest passages go to the model | 5 | next question |
| Minimum similarity score | Passages scoring below this are ignored; if none pass the assistant says it could not find the answer | 0.35 | next question |
| Recent turns sent with each question | How many latest question-and-answer pairs the model sees | 6 | next question |
| Chunk size / overlap | How documents are split when indexed (overlap must be smaller than size) | 900 / 150 | next indexing run |
| Questions per person per minute | Slows down anyone asking faster than this, in the Web UI, API and chat apps (0 = no limit); the API answers 429 with Retry-After | 20 | next question |

On first start these are copied from the matching `.env` lines (`TOP_K`, `SCORE_THRESHOLD`,
`MEMORY_MAX_TURNS`, `CHUNK_SIZE`, `CHUNK_OVERLAP`); after that the database is the source of truth and
the `.env` lines are ignored. More settings arrive with the features that use them.

## Scoped settings and answer traces

**Scoped settings** gives one knowledge base or department its own search settings. A knowledge base's value beats its
department's, and a department's beats **RAG settings**. The match threshold can only be set per knowledge base, because scores
from different embedding models are not comparable. **Reset** goes back to the inherited value. Changes apply within a few
seconds and are audited. Editors can change their own departments and those departments' knowledge bases.

In **Conversations**, **Why this answer?** opens an answer's trace: what was searched for, the passages found and whether they
were used, the settings in force, the prompt versions, the model that wrote the answer and the time each stage took. A passage
from a document you may not see shows as **Restricted**. Traces hold references only, are deleted with their conversation, and
each view is audited. Answers given before this feature have no trace. Editors see their own departments' conversations here, read-only.

## Languages and document summaries

Answers are always written in the language of the question; nothing is translated. When a question is too short to tell,
the conversation's last language is used, then the knowledge base's **Default language** (RAG settings or Scoped settings).
**Languages questions may be asked in** limits the languages: a question in another one gets a polite reply and nothing is
searched. A language can be added only when all its system messages are approved.

**Settings → Languages** shows, for the last 30 days, the answers per language, refused questions and answers that came out in
the wrong language. Its **Message catalogue** holds every system message per language. Add a language under RAG settings →
Message languages, then write its messages or **Draft missing messages with AI** and approve each one (have a native speaker
check them). A draft is never shown to anyone. The English blocked message stays on the Guardrails screen.

**Guardrails → Other languages** holds injection phrases, toxic words and banned topics for each language other than English,
and what happens to a language without lists: English checks with a warning (default), or block. Masking personal data follows
its own switch: turning the guardrails off no longer turns masking off.

For questions in several languages, switch the knowledge base to the local multilingual model in KB configuration and rebuild;
once the new version is in use, the screen offers the model's recommended match threshold (0.75).

**Document summaries:** each indexed document gets a 2 to 3 sentence summary in its own language, shown in KB documents
(Show more, Regenerate summary in the row menu, and the search box matches it). Admins can **Summarise existing documents** after
an estimate. Whole document text is sent to the preferred AI model. Summaries are display only and can be switched off per
knowledge base (Scoped settings) or for all (RAG settings).

## Glossary

**Input → Glossary** fixes vocabulary mismatch: people ask about "PTO", and the policy says "paid time off". Choose a
knowledge base or a department, **Start a draft**, add terms (the term, the other ways it is written, and its type), then
**Save draft** and **Publish**. Acronyms and synonyms work both ways. A preferred term only turns its aliases into the
preferred wording. Only the search changes: questions and answers keep their own wording, and nobody sees a document they
could not see before.

A knowledge base's terms win over its department's. **Test a question** shows the expansion without searching. Conflicts
(the same alias on two terms, or an alias that is itself a term) must be resolved before publishing. **Roll back** restores
an earlier version from the next question. Terms can be imported and exported as CSV.

The glossary is **off** until **Glossary** is set in Scoped settings (per knowledge base or department) or RAG settings. In
the "also add stable terms" mode, terms marked **Stable** are added to passages when they are indexed. The screen says how
many documents to re-index after a change.

## Suggested follow-up questions

After an answer from the documents, up to two follow-up questions can be shown as buttons. Each is checked first: it must not repeat
an earlier question, the person asking must be able to get it answered from documents they may see, and it must pass the
guardrails. It is **off** until **Suggest follow-up questions** is on for the channel *and* the knowledge base (Scoped settings;
Web UI is the channel "web"). Counts and the click rate are under **Reports → Suggested questions**. A button works only for the
person who asked, once, for 14 days (RAG settings). Slack needs **Interactivity** switched on in the Slack app (Socket Mode, no URL).
In Google Chat, switch **Suggestions as buttons** off on the channel to show them as text.

## LLM providers screen

**LLM providers** (earlier called AI models, and before that Providers) controls which AI models answer questions.
The screen shows **Which model answers** at the top, the **Connections** list below it, **Allowed providers** in the right-hand column,
and **Add connection** at the top right. Everything here applies within a few seconds,
with no restart.

1. **Approved providers.** Nothing is approved until an Admin chooses. Only approved types can be
   connected, because questions and document passages are sent to the provider. Grok (xAI) and DeepSeek
   carry a data-handling warning. A type cannot be un-approved while a provider of that type exists.
2. **Add a provider.** Pick an approved type, give it a name, paste the key. The key is **tested against
   the provider before anything is saved**; a failed test stores nothing. It is then stored encrypted and
   never shown again. Types: Anthropic, Google (Gemini), OpenAI, Azure OpenAI (endpoint, deployment, API
   version), Grok (xAI), DeepSeek, and any **OpenAI-compatible endpoint** (for example a gateway; if it
   cannot list its models, type them under Models).
3. **Model settings.** Choose the **preferred** model (it answers every question, including the
   search-or-chat decision) and an optional **fallback**, plus a request timeout (5 to 300 seconds). The
   screen warns if the fallback is on the same provider, because one outage or an exhausted account would
   stop both.

**When the fallback takes over.** If the preferred model fails with no credit (HTTP 402), a rate limit
(429), a server error (5xx), a timeout, or the provider being unreachable, the same question goes to the
fallback. A rejected request (any other 4xx) is **not** retried elsewhere: another model would reject it
too. A rejected or unauthorised key (401, 403) raises an alert but does not switch models. If both models
fail, the user gets the friendly "something went wrong" reply and an Admin alert is raised.

**Alerts.** Open alerts appear as banners above every screen: *provider failing* (credit exhausted, key
rejected), *fallback in use*, and *no model could answer*. They clear themselves when the provider answers
a real question again, or you can dismiss them. **Test connection** checks the key and the address by
listing the provider's models; that needs no credit, so a passing test does **not** prove an account with
no credit can still answer, and it does not clear a credit alert.

**First start after the upgrade.** The model that used to be set in `.env` (`NINEROUTER_BASE_URL`,
`NINEROUTER_API_KEY`, `LLM_MODEL`) is imported once as a provider named *Default (from .env)* (an
OpenAI-compatible endpoint), approved, and made the preferred model. From then on the key lives only in
the encrypted store: delete those `.env` lines; they are ignored.

**Usage.** Every model call (the search-or-chat decision and the answer, successful or failed) is recorded
with provider, model, tokens, and a cost when the model has a price. No prices are seeded; a price table
and the Usage & Cost screen arrive later. Only the error type and HTTP status of a failure are stored,
never the message.

## Security model

- **Local only.** The page and API answer only requests that come directly from this machine.
  Requests carrying proxy or tunnel headers (`X-Forwarded-For`, `CF-Connecting-IP`, ...) or
  addressed to another host name get 403, because the same server is exposed to Teams and
  Google Chat through a tunnel. `ADMIN_ALLOW_REMOTE=true` disables this check; only do that
  behind HTTPS and your own access controls.
- **Sign-in.** Passwords are stored as scrypt hashes. Five wrong passwords lock that account
  for five minutes (and 20 failures lock an address). Login errors are identical for an unknown
  user and a wrong password.
- **Sessions.** A signed cookie (`HttpOnly`, `SameSite=Strict`, 8 hours), plus a CSRF header on
  every change. Changing the password, or the first key in `APP_SECRET_KEY`, signs everyone out.
- **Secrets.** Encrypted at rest with the master key; returned by nothing; removed from error
  messages and never written to the audit log.
- **Browser protections.** The page ships with a Content-Security-Policy that forbids inline
  script and style and framing, and it builds content without HTML strings.
- **CORS.** No browser origin is trusted unless listed in `CORS_ORIGINS`.
- **Chat API.** `/chat` and `/conversations` need `Authorization: Bearer <CHAT_API_TOKEN>`; with no token
  configured they refuse everything. Chat apps do not use this path (Slack, Teams and Google Chat reach
  the assistant directly, and their webhooks are checked by the platform's own signature).
- **API docs.** `/docs` and `/openapi.json` open only from this machine, never through a tunnel or proxy.
  A test asserts that every route refuses an anonymous request, apart from `/health`.

## Settings (`.env`)

| Variable | Meaning | Default |
|---|---|---|
| `APP_SECRET_KEY` | Master key(s), comma-separated. First encrypts; all can decrypt. Required for the admin features. | none |
| `ADMIN_DB_PATH` | Admin database file | `./admin_data/admin.db` |
| `ADMIN_EMAIL_DOMAIN` | Only this domain can be admins (empty = any) | `sysfore.com` |
| `ADMIN_ALLOW_REMOTE` | Allow non-local admin access (see above) | `false` |
| `CORS_ORIGINS` | Browser origins allowed to call the API | none |
| `CHAT_API_TOKEN` | Service token for the chat API (required; for service callers) | none (API locked) |
| `DATABASE_URL` | PostgreSQL URL instead of the SQLite file, e.g. `postgresql+psycopg://user:pass@host/db` | none |

Without `APP_SECRET_KEY` the admin API answers 503 and the server runs channels from
`channels.yaml` as before.

## Master key: backup and rotation

The master key (`APP_SECRET_KEY`) encrypts every stored credential (channel secrets, provider keys, knowledge base keys,
notification addresses) and signs sign-in sessions. Without it those credentials cannot be read.

**Backup**
1. Store a copy of `APP_SECRET_KEY` somewhere other than the server and other than the database backup (a password manager or
   key vault). A database backup without the key restores everything except the credentials.
2. Note the key's fingerprint next to the copy: `python -m app.secretstore fingerprint` prints a short tag per configured key
   (never the key itself). Run it later on the restored key to confirm that the copy is the right one.
3. Back up `admin_data/admin.db` as before (with the server stopped, or with the SQLite backup command).
4. Before a risky change, run `python -m app.secretstore check`: it tries to decrypt every stored secret and reports any that cannot be read.

**Rotation** (for example when someone who knew the key leaves)
1. `python -m app.secretstore generate` and put the new key **in front**: `APP_SECRET_KEY=<new>,<old>`. Restart.
2. Back up the database, then run `python -m app.secretstore rotate`. It re-encrypts every stored secret with the new key in one
   step; if any secret cannot be decrypted nothing is changed and it says so.
3. Run `python -m app.secretstore check`: it should report `0 still use an older key`.
4. Remove the old key from `APP_SECRET_KEY` and restart. Keep the old key's backup until you have confirmed everything works.

Effects to expect: everyone is signed out when the first key changes (sessions are signed with it), and exports made after
the change use different pseudonyms for people than earlier exports.

**Stored files** use the same key. `check` also reports how many stored files are not encrypted yet, and `rotate`
re-encrypts every stored file before the secrets (it checks every file first and changes nothing if one cannot be read).
A file backup without the key is unreadable, so keep the key's backup apart from both.

## Stored files

Uploaded files are encrypted with `APP_SECRET_KEY` before they are written, on local disk (`DOCS_DIR`) or in Azure Blob
Storage (`FILE_STORE=azure_blob`, `AZURE_BLOB_CONNECTION_STRING`, `AZURE_BLOB_CONTAINER`; install with
`pip install -e .[azure-blob]`). While a file is parsed it is decrypted into `DOCS_DIR/.work` and deleted straight after;
the server wipes that folder at start.

- `python -m app.knowledge.filestore status`: how many stored files are still unencrypted.
- `python -m app.knowledge.filestore encrypt`: encrypt them (the server also does this at start).
- `python -m app.knowledge.filestore secure <folder>`: encrypt every file of another folder in place, for example the old
  `chats` backup. The server can still import from a secured folder.

While unencrypted files remain (in the store, `data/docs` or the old chat files) the Home screen shows an alert.
Moving existing files from local disk to a blob container is not automated yet: copy the folder's files to the container
with the same names (they are already encrypted).

## Backup and recovery

- Back up `APP_SECRET_KEY` (see above) and `admin_data/admin.db` (plus the `-wal` file if the server is
  running; the simplest safe backup is while it is stopped).
- Lost key: stored secrets are unrecoverable. Re-enter them in the page.
- Forgot the admin password: `python -m app.admin.cli set-password you@sysfore.com`.

## Admin API (what the page calls)

All under `/admin/api`, JSON, cookie + `X-CSRF-Token` header for changes.

| Method and path | Purpose |
|---|---|
| `POST /auth/login`, `/auth/logout`, `GET /auth/me`, `POST /auth/password` | Sign in and out, session info, change password |
| `GET /channels`, `GET /channels/{type}` | Channel list/detail with secrets masked |
| `PUT /channels/{type}` | `{enabled, values}`: validate, start, then save |
| `POST /channels/{type}/test` | `{values}`: test connection |
| `DELETE /channels/{type}` | Remove a channel |
| `GET /audit?limit=` | Audit log |
| `GET /me/permissions` | Screens this person may see |
| `POST /auth/set-password` | `{token, password}`: choose a password with a one-time link (no sign-in) |
| `GET/POST /users`, `PUT /users/{id}`, `POST /users/{id}/reset-link` | People, roles, status, links (Admin) |
| `GET/POST /departments`, `PUT/DELETE /departments/{id}` | Departments (Editors: own departments, topics and retention only) |
| `POST /chat-auth/login`, `/chat-auth/set-password`, `GET /chat-auth/me` (not under `/admin/api`) | Web UI sign-in for chat users |
| `GET/POST /kbs`, `PUT/DELETE /kbs/{id}` | Knowledge bases (Editors: their departments only) |
| `GET/POST /kbs/{id}/documents`, `POST /documents/{id}/replace`, `/reindex`, `/move`, `DELETE /documents/{id}`, `POST /kbs/{id}/reindex` | Documents (upload is multipart) |
| `GET/POST /groups`, `PUT/DELETE /groups/{id}`, `GET /directory` | Groups and the people list |
| `GET/PUT /documents/{id}/access` | Who can see a document (`visibility`: everyone, people, groups) |
| `GET/PUT /kbs/{id}/access` | Who may ask a knowledge base (empty = everyone on a bound channel) |
| `GET /prompts`, `GET /prompts/versions`, `POST /prompts/drafts`, `POST /prompts/versions/{id}/publish`, `/rollback`, `POST /prompts/preview` | Prompts (base: Admin only; department: its Editors and Admins) |
| `GET/PUT /guardrails` (injection, toxicity and leak controls included) | Guardrail settings (Admin only); banned topics are edited per department |
| `GET /conversations`, `GET/DELETE /conversations/{id}` (under `/admin/api`, Admin only) | Browse, open and delete stored conversations; opening and deleting are audited |
| `GET /conversations/retention/status`, `POST /conversations/retention/run`, `POST /conversations/export` (Admin only) | Retention status and run, and the audited export (off by default) |
| `POST /kbs/{id}/chunk-preview` (multipart: file, strategy, chunk_size, chunk_overlap) | Preview the chunks a strategy makes from a sample file; nothing is saved |
| `POST /kbs/{id}/embedding/test`, `POST /kbs/{id}/embedding/download` (Admin) | Try an embedding model without saving; download the local multilingual model |
| `POST /kbs/{id}/store/test` | Try a vector store's connection without saving anything |
| `GET /usage?group=&date_from=&date_to=`, `GET/PUT /prices`, `DELETE /prices/{id}` | Usage and cost (Editors: own departments); prices are Admin only |
| `GET/PUT /alert-sinks`, `POST /alert-sinks/test` | Optional webhook and email notifications for new alerts (Admin only) |
| `GET /me/alerts`, `POST /me/alerts/{id}/resolve` | Alerts meant for you (Admins: all) |
| `GET /audit/search?actor=&action=&target=&date_from=&date_to=&limit=&offset=` | Filtered, paged audit log (Admin only) |
| `GET /reports/guardrails`, `/reports/knowledge` (date range), `/reports/channels` | Reports (Admin only) |
| `GET/POST /admins`, `DELETE /admins/{email}` | Older admin endpoints, kept for compatibility |

## Running the admin on its own port, and remote access

Set `ADMIN_PORT=8090` in `.env` and restart. The admin page is then at `http://127.0.0.1:8090/admin`, and the main port
(webhooks, `/chat`, `/health`) serves no admin route, so a tunnel or reverse proxy pointed at the main port cannot reach it.
Use one server process (the admin listener lives inside it).

To let Editors reach it from other machines:

1. Keep `ADMIN_HOST=127.0.0.1` and put an HTTPS reverse proxy on the same machine (or set `ADMIN_HOST=0.0.0.0` behind a firewall).
2. Set `ADMIN_ALLOW_REMOTE=true`. Requests that do not arrive over HTTPS are then refused (the proxy must send `X-Forwarded-Proto: https`).
3. Only if the port is reachable from this machine alone (for example Docker publishing to 127.0.0.1) you may set `ADMIN_REQUIRE_HTTPS=false`.

Example (nginx):

```
server {
  listen 443 ssl;
  server_name admin.example.com;
  # ssl_certificate / ssl_certificate_key ...
  location / {
    proxy_pass http://127.0.0.1:8090;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
  }
}
```

## Known limits

- Sign in with Microsoft (Entra ID, one organisation) or Google (one Workspace domain) is available for the admin
  center (Settings → Sign-in; USER_MANUAL.md section 1.1) and needs an app registration / OAuth client. The Web UI
  chat still uses email and password.
- Sessions cannot be revoked one by one. They end on expiry, password change, key change, or when an Admin unlinks
  the person's Microsoft or Google account (which ends all of that person's sessions).
- In Docker the admin page is refused, because the container sees the Docker network and not localhost,
  unless `ADMIN_ALLOW_REMOTE=true`. The remote-admin design (HTTPS, separate port) is a later phase.
  The compose file carries `APP_SECRET_KEY`, the chat token and an `admin_data` volume.
