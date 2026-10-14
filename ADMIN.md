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

## Settings (`.env`)

| Variable | Meaning | Default |
|---|---|---|
| `APP_SECRET_KEY` | Master key(s), comma-separated. First encrypts; all can decrypt. Required for the admin features. | none |
| `ADMIN_DB_PATH` | Admin database file | `./admin_data/admin.db` |
| `ADMIN_EMAIL_DOMAIN` | Only this domain can be admins (empty = any) | `sysfore.com` |
| `ADMIN_ALLOW_REMOTE` | Allow non-local admin access (see above) | `false` |
| `CORS_ORIGINS` | Browser origins allowed to call the API | none |

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
| `GET/POST /admins`, `DELETE /admins/{email}` | Manage admins |

## Known limits

- Local password sign-in only; Microsoft Entra ID sign-in is planned and needs an app
  registration.
- Sessions cannot be revoked individually (they end on expiry, password change or key change).
- Docker is not set up for the admin database (`APP_SECRET_KEY` and a volume for `admin_data/`
  are not in `docker-compose.yml` yet).
- Upcoming (greyed out in the sidebar, not built yet): documents and per-document access,
  chunking strategy (fixed size, per page, by heading, semantic), vector store choice (pgvector,
  Qdrant, Azure AI Search), retrieval strategy (hybrid keyword + vector, re-ranker, query
  rewriting), and LLM provider and model (Claude, Azure, Ollama, a model per step, fallback).
  Later: multiple knowledge bases.
