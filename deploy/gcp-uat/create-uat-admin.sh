#!/usr/bin/env bash
# Creates the shared UAT admin account (until Microsoft / Google sign-in is set up).
# The password is generated here, kept only in Secret Manager, and never printed. Read it with:
#   gcloud secrets versions access latest --secret=caa-uat-admin-password --project=sysfore-nexus
# Safe to run again: an existing secret is reused and an existing account is left as it is.
# The shared account (agent-admin@sysfore.in) predates the switch to sysfore.com; the job allows its own domain.
set -euo pipefail

P=sysfore-nexus
R=asia-south1
EMAIL=${1:-agent-admin@sysfore.in}
APP_SA=caa-uat-app@$P.iam.gserviceaccount.com
IMAGE=$(gcloud run services describe caa-uat-app --project=$P --region=$R --format="value(spec.template.spec.containers[0].image)")

if ! gcloud secrets describe caa-uat-admin-password --project=$P >/dev/null 2>&1; then
  python -c "import secrets; print(secrets.token_urlsafe(15), end='')" \
    | gcloud secrets create caa-uat-admin-password --project=$P --replication-policy=user-managed --locations=$R --data-file=-
fi
gcloud secrets add-iam-policy-binding caa-uat-admin-password --project=$P \
  --member=serviceAccount:$APP_SA --role=roles/secretmanager.secretAccessor --quiet --format=none

# A one-off job on the app's image, service account and database connection. "^;^" makes ";" the list separator,
# so the Python below can contain commas.
CODE='import os
from app.config import settings
from app.db import get_db
from app.admin import users
db = get_db()
email = os.environ["ADMIN_EMAIL"]
if any(u["email"] == email for u in users.list_admins(db)):
    print("Admin already exists:", email)
else:
    users.create_admin(db, email, os.environ["ADMIN_PASSWORD"], domain=settings.admin_email_domain)
    print("Created admin:", email)'

gcloud run jobs deploy caa-uat-create-admin --project=$P --region=$R --image=$IMAGE \
  --service-account=$APP_SA --cpu=1 --memory=1Gi --max-retries=0 --task-timeout=300 \
  --set-cloudsql-instances=$P:$R:caa-uat-db \
  --set-secrets=APP_SECRET_KEY=caa-app-secret-key:latest,DATABASE_URL=caa-database-url:latest,ADMIN_PASSWORD=caa-uat-admin-password:latest \
  --set-env-vars=ADMIN_EMAIL=$EMAIL,ADMIN_EMAIL_DOMAIN=${EMAIL#*@} \
  --command=python --args="^;^-c;$CODE" \
  --execute-now --wait

gcloud logging read "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"caa-uat-create-admin\" AND textPayload:\"admin\"" \
  --project=$P --limit=3 --freshness=15m --format="value(textPayload)"

echo
echo "Sign in at https://caa-uat-app-251420217705.asia-south1.run.app/admin as $EMAIL"
echo "Password: gcloud secrets versions access latest --secret=caa-uat-admin-password --project=$P"
