# Run it, test it, push it

Step-by-step. Commands are for Windows PowerShell; `<venv>` means `$env:USERPROFILE\rag-venv`.

## 1. Set up once

1. Install Python 3.11. Create the environment **outside OneDrive** (long paths break pip there):
   ```powershell
   py -3.11 -m venv --without-pip $env:USERPROFILE\rag-venv
   py -3.11 -m pip --python $env:USERPROFILE\rag-venv\Scripts\python.exe install -e .
   ```
2. Copy `.env.example` to `.env`. Fill in `NINEROUTER_BASE_URL`, `NINEROUTER_API_KEY`, `LLM_MODEL` (the first model; afterwards providers
   are managed on the admin page) and the two required secrets:
   ```powershell
   & $env:USERPROFILE\rag-venv\Scripts\python.exe -m app.secretstore generate        # paste as APP_SECRET_KEY
   & $env:USERPROFILE\rag-venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as CHAT_API_TOKEN
   ```
   **Keep a copy of `APP_SECRET_KEY` somewhere else** (see "Master key" in `ADMIN.md`).
3. Put the databases and the vector store outside OneDrive: set `ADMIN_DB_PATH`, `CHROMA_PATH` and `CHAT_DIR` in `.env` to a local folder.
4. Create the first admin (it asks for a password without showing it):
   ```powershell
   & $env:USERPROFILE\rag-venv\Scripts\python.exe -m app.admin.cli create-admin you@sysfore.com
   ```

## 2. Run the application

```powershell
.\start.ps1            # server on http://127.0.0.1:8000 (the database upgrades itself on start)
.\start.ps1 -Ingest    # index everything in data\docs first
.\start.ps1 -Tunnel    # also open a public HTTPS address for Teams / Google Chat (needs cloudflared);
                       # the admin page then moves to http://127.0.0.1:8001/admin so it is never tunnelled
```

Check it: <http://127.0.0.1:8000/health> should answer `{"status":"ok", ...}`.

Web chat page (optional, a separate process): `streamlit run streamlit_app.py` then open <http://localhost:8501>.

## 3. First use, on the admin page (<http://127.0.0.1:8000/admin>)

1. **Providers**: approve a provider type, add your key (it is tested before it is saved), then set the preferred and, if you have one, a fallback model.
2. **Departments** and **Users**: add people; share the one-time set-password link shown on screen with each person.
3. **Knowledge bases**: create one, choose chunking, upload documents, wait for *Indexed*, set who may see each document.
4. **Channels**: enter and test the Slack credentials (Teams and Google Chat: see `PLATFORM_RUNBOOK.md`), bind a knowledge base, switch on.
5. **Prompts** and **Guardrails**: review the defaults. **Usage and cost**, **Reports** and **Audit log** show what happened.

## 4. Run it in Docker instead

```powershell
docker compose build          # first build installs every package: several minutes
docker compose up -d
curl http://127.0.0.1:8000/health
docker compose logs -f        # Ctrl+C to stop following
docker compose down           # stop (data stays in the volumes)
```

The container restarts itself after a crash (`restart: unless-stopped`) and is checked every 30 seconds. The admin page is on port 8090,
published on this machine only (see `DEPLOYMENT.md` before exposing anything).

## 5. Update a running server

1. Stop writes if you can, then **back up the database** (the SQLite backup command keeps it consistent):
   ```powershell
   & $env:USERPROFILE\rag-venv\Scripts\python.exe -c "import sqlite3,time; s=sqlite3.connect('admin_data/admin.db'); d=sqlite3.connect('admin_data/admin.db.bak-'+time.strftime('%Y%m%d-%H%M%S')); s.backup(d); d.close()"
   ```
2. Restart (`.\start.ps1`, or `docker compose up -d --build`). New migrations apply on start.
3. Check `/health`, sign in, and ask one question.

## 6. Run the tests

They use temporary data and never touch your real database or documents.

```powershell
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m pytest                      # everything, on SQLite (about 7 minutes)
$env:TEST_DB = "postgres"; & $env:USERPROFILE\rag-venv\Scripts\python.exe -m pytest   # on PostgreSQL (needs Docker; starts a throwaway container)
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m pytest tests\test_security_pass.py   # one file
& $env:USERPROFILE\rag-venv\Scripts\python.exe -m pytest -m live tests\test_live_platforms.py   # needs real Teams / Google credentials (see the file)
```

## 7. Push your work to a new branch

1. Look at what changed and make sure no secret is included (`.env`, `admin_data/`, `chats/`, `chroma_db/`, `models/` and database backups are ignored):
   ```powershell
   git status
   git diff --stat
   ```
2. Create the branch, commit and push (the first push sets the upstream):
   ```powershell
   git switch -c my-new-branch
   git add -A
   git commit -m "Describe the change in one line"
   git push -u origin my-new-branch
   ```
3. Open a pull request on GitHub from `my-new-branch` into `main`. Later pushes on the same branch are just `git push`.

If `git push` asks you to sign in, use your GitHub account or a personal access token (a token with the `repo` scope).
Never commit `.env` or paste a key into a file that is tracked.
