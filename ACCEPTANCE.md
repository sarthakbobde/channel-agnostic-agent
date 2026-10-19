# Final acceptance report

Date: 19 October 2026. Plan: `docs/development-plan-v3.md`, phases 0 to 11. Per-requirement evidence is in `TRACEABILITY.md`.

## Where things stand

| Item | State |
|---|---|
| BRD requirements (82) | 75 built, 6 partial, 1 not built (at the start: 17, 21, 44) |
| Automated tests | SQLite: 298 passed, 11 skipped. PostgreSQL: 297 passed, 12 skipped. Two live-platform tests are left out by default (`-m live`) |
| What the tests include | unit and integration tests, 16 phase feature checks, 3 end-to-end scenarios, a security pass (23 tests), exposure and hardening tests |
| Live server (this machine) | running on `127.0.0.1:8000`, database at migration 0022, `/health` ok, 3 stored secrets all decrypt with the current key, document search answers |
| Live answers | **not working until the model provider has credit.** The only provider is the Gemini key from `.env`, which answers HTTP 402 (prepayment depleted) |
| Branch | `phases-0-11-complete` on GitHub (`origin`) |

## What only a person can do

| # | Action | Who | Why it cannot be done here |
|---|---|---|---|
| 1 | Add credit to the Gemini account, or add another provider on the Providers tab (the fallback model then takes over automatically) | Account owner | The live system cannot answer without a working model |
| 2 | Add Slack scopes `users:read` and `users:read.email`, then reinstall the app | Slack workspace admin | Without them people are unknown Viewers and see only Everyone documents |
| 3 | Create the Azure Bot, enable the Teams channel, build and sideload the Teams app, point the endpoint at a public HTTPS address, then enter the credentials on the Channels tab and press Test | Azure owner, Teams admin | Needs a tenant (see `PLATFORM_RUNBOOK.md`) |
| 4 | Create the Google Chat app in a Workspace, choose the audience, enter it and press Test | Workspace admin | Needs a Workspace |
| 5 | Provide a public HTTPS address and reverse proxy for production | Infrastructure | See `DEPLOYMENT.md` |
| 6 | Keep a copy of `APP_SECRET_KEY` outside the server, with its fingerprint (`python -m app.secretstore fingerprint`) | Owner | Lost key means every stored credential must be re-entered |
| 7 | Rotate secrets that passed through chat or tool output (the Gemini key, the Slack tokens); the old lines in `.env` can then go | Owner | Policy decision |
| 8 | Remove the `testadmin@sysfore.com` account and delete `admin_data/testadmin-credentials.txt` | Admin | Test leftovers |
| 9 | Move `chroma_db`, `chats` and `admin_data` out of OneDrive | Owner | OneDrive sync can lock SQLite files |
| 10 | Privacy sign-off before turning conversation export on (it is off) | Privacy owner | Decision D7 |
| 11 | Try the keyed services with real keys: Azure AI Search, OpenAI / Azure OpenAI / Cohere embeddings, Anthropic, Azure OpenAI chat | Owner | They are built to each service's documented format and tested against stand-ins only |
| 12 | ~~Rebuild the Docker image and repeat the restart check~~ **Done on 19 October.** The rebuild first failed because two pinned packages conflicted; fixed in `bce0759`. On the new image (541 MB) a killed server was restarted by the container, `docker restart` came back healthy, the data and migration 0022 survived both, and a manual stop stayed stopped. A blank install logs one harmless traceback at start-up: a Slack channel seeded from the environment with no tokens fails to start (other channels are unaffected) | Owner | |
| 13 | Docker housekeeping: a 631 MB pgvector image and `postgres:16-alpine` are cached, and Docker Desktop is running | Owner | Disk space, only if wanted |

## Decisions still marked Proposed

These are built as written but nobody has confirmed them (`DECISIONS.md`): D2 and D2b (Web UI sign-in; one-time set-password links
instead of email invitations), D3 (no single sign-on with Entra ID), D5, D6, D8, D9 (document statuses), D11 (one process, database job lock),
D12 (SQLite by default, tested on PostgreSQL too), D13 (unknown sender is a Viewer in the default department), D14 (both models fail:
friendly reply and alert), D16 (redaction allow-list), D17 (access needs both the knowledge base's permission and the document's).

Three of them differ from the text in `DECISIONS.md`:
- **D5:** several knowledge bases are merged by ranking. "Choose by the asker's department" is not a separate mode; who may ask which
  knowledge base is set by that knowledge base's query groups.
- **D6:** prices are typed in by hand; none are seeded. A call without a price is counted and shown as unpriced on the Usage and cost
  tab, but it does not raise an alert.
- **D8:** the optional notifications are an https webhook and email (Settings, off by default).

## Known limitations

- **#7 Display Name** for channels is not built (the channel type is the label).
- **Injection and toxicity checks are rule-based** (known phrasing and word lists). Paraphrases and other languages can slip through.
  Model-based detection was considered and dropped from the plan (LLM Guard was rejected: unmaintained, and it forced older versions of other libraries).
- **Google Chat** answers inside 25 seconds or sends a polite message; late answers need a Google service account (D10).
- **Single process:** the question limit (20 per person per minute, adjustable), the scheduler and the admin listener assume one server
  process. Several instances would need a shared store for the limiter and a leader for the jobs.
- **Not set up as a Windows service.** Auto-restart exists for the container only.
- **Sessions** end after 8 hours, on a password change or on a key change; individual sessions cannot be revoked. Rotating the master key
  signs everyone out and changes the pseudonyms used in later conversation exports.
- **Embeddings by API and Azure AI Search** are untested against the real services (action 11 above). pgvector was verified for real.
- **Teams:** Microsoft's own token library handles the good-token path; the tests prove forged and missing tokens are refused, and the
  rest is proven only by a real Teams message. Reading a sender's email from the Teams roster is unverified on a real tenant.
- **Admin page** was checked by hand on desktop and phone widths; there is no automated browser test.
- **Conversations from before the database move** are imported once; the old Web UI conversations may still need a look.

## The live audit log

On 16 October earlier checks accidentally wrote 31 rows into the live audit log (knowledge-base rebuilds of "HR docs" and "IT help",
which do not exist live, and set-password rows for two test users). At the owner's request they were removed on 19 October after a
backup (`admin_data/admin.db.bak-pre-auditclean-*`), and the removal itself is recorded as an `audit.cleanup` row. The 25 earlier rows
remain, including the sign-ins and failed sign-in made with the `testadmin@sysfore.com` and `nobody@` test accounts; those were real
requests to the live system. The checks now run in throwaway folders, so it cannot happen again.
