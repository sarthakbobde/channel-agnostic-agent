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
| **Account** | Change your password. This signs out every other session. |

Teams and Google Chat show the endpoint path to enter at the provider. They need a public HTTPS
address (for example `.\start.ps1 -Tunnel`); Slack uses Socket Mode and needs none.

## Settings screen

**Settings** holds values the assistant reads while it runs. Changes apply within a few seconds, with
no restart. Each field shows its allowed range and what it does; a value outside the range is refused
with a message and nothing changes. Every change is written to the audit log (old value to new value).

| Setting | Meaning | Default | Applies |
|---|---|---|---|
| Passages retrieved per question | How many of the closest passages go to the model | 5 | next question |
| Minimum similarity score | Passages scoring below this are ignored; if none pass the assistant says it could not find the answer | 0.35 | next question |
| Recent turns sent with each question | How many latest question-and-answer pairs the model sees | 6 | next question |
| Chunk size / overlap | How documents are split when indexed (overlap must be smaller than size) | 900 / 150 | next indexing run |

On first start these are copied from the matching `.env` lines (`TOP_K`, `SCORE_THRESHOLD`,
`MEMORY_MAX_TURNS`, `CHUNK_SIZE`, `CHUNK_OVERLAP`); after that the database is the source of truth and
the `.env` lines are ignored. More settings arrive with the features that use them.

## Providers screen

**Providers** controls which AI models answer questions. Everything here applies within a few seconds,
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
| `CHAT_API_TOKEN` | Service token for the chat API (required; the Streamlit page sends it) | none (API locked) |
| `DATABASE_URL` | PostgreSQL URL instead of the SQLite file, e.g. `postgresql+psycopg://user:pass@host/db` | none |

Without `APP_SECRET_KEY` the admin API answers 503 and the server runs channels from
`channels.yaml` as before.

## Key rotation

1. Generate a new key and put it **in front** of the old one: `APP_SECRET_KEY=<new>,<old>`.
2. Restart. New secrets use the new key; old ones still decrypt. (A bulk re-encrypt command is
   not built yet. A secret is re-encrypted with the new key only when you enter its value again
   in the page; leaving the field blank keeps the old encrypted copy.)
3. Once every secret has been re-entered, remove the old key.

## Backup and recovery

- Back up `APP_SECRET_KEY` and `admin_data/admin.db` (plus the `-wal` file if the server is
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

- Local password sign-in only; Microsoft Entra ID sign-in is planned and needs an app
  registration.
- Sessions cannot be revoked individually (they end on expiry, password change or key change).
- In Docker the admin page is refused, because the container sees the Docker network and not localhost,
  unless `ADMIN_ALLOW_REMOTE=true`. The remote-admin design (HTTPS, separate port) is a later phase.
  The compose file carries `APP_SECRET_KEY`, the chat token and an `admin_data` volume.
- Upcoming (greyed out in the sidebar, not built yet): documents and per-document access,
  chunking strategy (fixed size, per page, by heading, semantic), vector store choice (pgvector,
  Qdrant, Azure AI Search), and retrieval strategy (hybrid keyword + vector, re-ranker, query
  rewriting). Later: multiple knowledge bases, a different model per step.
