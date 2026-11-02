# UAT on Google Cloud (project `sysfore-nexus`, region `asia-south1`)

| | Address |
|---|---|
| Admin center | https://caa-uat-app-251420217705.asia-south1.run.app/admin |
| Web UI | https://caa-uat-web-251420217705.asia-south1.run.app |
| Teams messaging endpoint | https://caa-uat-app-251420217705.asia-south1.run.app/channels/teams/messages |

## What runs where

| Resource | Name | Notes |
|---|---|---|
| Cloud Run | `caa-uat-app` | API, Teams, Google Chat, admin center. 1 vCPU / 2 GiB, CPU on while running (Teams replies and indexing finish after the request), scales 0 to 1. |
| Cloud Run | `caa-uat-web` | Web chat (Chainlit), same image. 512 MiB, request-based billing, scales 0 to 1. |
| Cloud SQL | `caa-uat-db` | PostgreSQL 16, `db-f1-micro`, database `caa`, user `caa_app`. Admin data and the search index (pgvector). Deletion protection on. |
| Cloud Storage | `sysfore-nexus-caa-uat-data` | Mounted at `/mnt/data`: uploaded documents (`kb_files/`) and chat history (`chats/`). |
| Cloud Storage | `sysfore-nexus-caa-uat-builds` | Build uploads, deleted after 7 days. |
| Artifact Registry | `caa-uat` | The image (`app:<commit>`); the 5 newest are kept. |
| Secret Manager | `caa-app-secret-key`, `caa-chat-api-token`, `caa-database-url`, `caa-db-password`, `caa-uat-admin-password`, `caa-chainlit-auth-secret` | Generated, never printed. **Back up `caa-app-secret-key`**: without it the stored credentials cannot be decrypted. |
| Service accounts | `caa-uat-app`, `caa-uat-web`, `caa-uat-build` | Least privilege: the app reads its own secrets, the data bucket and Cloud SQL; the web UI has no roles. |

Knowledge bases must use the **PostgreSQL + pgvector** vector store (connection string empty). New ones start on it
automatically because the admin database is PostgreSQL. Chroma would live on the container's temporary disk and be
lost whenever the app scales to zero; the default knowledge base created on the first deploy (before this became
automatic) is on Chroma, so switch it to pgvector or leave it unused.

## Deploy a new version

From the repository root, in Git Bash:

```bash
gcloud builds submit . --project=sysfore-nexus --region=asia-south1 --config=deploy/gcp-uat/cloudbuild.yaml \
  --substitutions=_TAG=$(git rev-parse --short HEAD) \
  --service-account=projects/sysfore-nexus/serviceAccounts/caa-uat-build@sysfore-nexus.iam.gserviceaccount.com \
  --gcs-source-staging-dir=gs://sysfore-nexus-caa-uat-builds/source
bash deploy/gcp-uat/deploy-services.sh
```

The build uploads the working tree minus `.gcloudignore` (no `.env`, local database or data). The database
schema is migrated automatically when the app starts.

## Admin access

Until Microsoft sign-in is set up, there is one shared admin, `agent-admin@sysfore.in`, created by
`create-uat-admin.sh`. Its password is only in Secret Manager:

```bash
gcloud secrets versions access latest --secret=caa-uat-admin-password --project=sysfore-nexus
```

When Admins can sign in with Microsoft (Settings > Sign-in), deactivate that account under Users and delete the
secret.

## Teams

See the "UAT on Google Cloud Run" section of [TEAMS_SETUP.md](../../TEAMS_SETUP.md).

## Costs and pausing

About $25-30 a month, mostly Cloud SQL. Between test rounds the database can be stopped (storage only, about $2):

```bash
gcloud sql instances patch caa-uat-db --project=sysfore-nexus --activation-policy=NEVER    # stop
gcloud sql instances patch caa-uat-db --project=sysfore-nexus --activation-policy=ALWAYS   # start
```

The app scales to zero by itself. The first request after a quiet spell takes 10-20 seconds; opening
`/health` before a session wakes it.
