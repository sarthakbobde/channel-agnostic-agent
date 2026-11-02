# Working on this repository (Claude Code)

The build plan is `../docs/development-plan-v4_3-release-2 1.md` (Release 2), on top of the v3 plan. Its Part A rules apply to
every change:

- **One core path.** Presenters only format. Every button click goes through `app/channels/core.handle_action()`, and a
  suggestion click becomes an ordinary `core.handle()` question.
- **Settings, not code branches.** Features are switched and tuned through `app/scoped_settings.py`: channel, then KB, then
  department, then global. Each key declares the scopes it allows. The defaults are safe: new features are off.
- **Search text is separate from question text.** Only the search changes, through the glossary or the router. The question
  in the prompt, the memory, the answer and the trace stays as the person wrote it.
- **Permissions first.** Glossary expansion, suggestions and traces use the asker's allow-list (`app/knowledge/access.py`),
  with no bypass.
- **Privacy.** Masking runs on its own switch (D36). Traces store references, not passage text. Suggestions are stored masked.
  Free text never goes into logs or audit details.
- **Code-owned prompt text.** Anything the code parses (the not-found marker, the suggestion block, the language directive)
  lives in code, never in an editable prompt.
- **Signed payloads** (D37), with a key derived from `APP_SECRET_KEY`.

How work is done here:
- One branch per phase (`r2/phase-<n>-<topic>`).
- Tests on SQLite and PostgreSQL (`TEST_DB=postgres`, which needs Docker).
- A Phase Report in `PROGRESS.md`, and decisions and deviations in `DECISIONS.md`.
- Stop for review after each phase.
- Commit, push and deploy only when asked. Never commit `.env`, `admin_data/` or `models/`.
