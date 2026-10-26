#!/usr/bin/env bash
# Deploys (or updates) the two UAT Cloud Run services from an image already built by cloudbuild.yaml.
#   bash deploy/gcp-uat/deploy-services.sh            # the image tagged with the current commit
#   bash deploy/gcp-uat/deploy-services.sh cfdc613    # a specific tag
set -euo pipefail
# Git Bash on Windows rewrites arguments such as mount-path=/mnt/data into Windows paths. Exclude only the flags that
# carry container paths: switching conversion off entirely also breaks gcloud's own launcher.
export MSYS2_ARG_CONV_EXCL="--add-volume-mount;--startup-probe;--set-env-vars"

P=sysfore-nexus
R=asia-south1
N=251420217705             # project number, part of the run.app addresses
TAG=${1:-$(git rev-parse --short HEAD)}
IMAGE=$R-docker.pkg.dev/$P/caa-uat/app:$TAG
APP_URL=https://caa-uat-app-$N.$R.run.app
WEB_URL=https://caa-uat-web-$N.$R.run.app

echo "Deploying $IMAGE"

# API, Teams, Google Chat and the admin center (/admin). CPU stays on while an instance runs, because Teams replies
# and document indexing finish after the request; it still scales to zero when idle. Max 1: caches live in memory.
gcloud run deploy caa-uat-app --project=$P --region=$R --image=$IMAGE \
  --service-account=caa-uat-app@$P.iam.gserviceaccount.com \
  --port=8000 --cpu=1 --memory=2Gi --no-cpu-throttling --cpu-boost \
  --min-instances=0 --max-instances=1 --concurrency=80 --timeout=300 \
  --execution-environment=gen2 --allow-unauthenticated --ingress=all \
  --add-cloudsql-instances=$P:$R:caa-uat-db \
  --add-volume="name=data,type=cloud-storage,bucket=sysfore-nexus-caa-uat-data,mount-options=uid=10001;gid=10001" \
  --add-volume-mount=volume=data,mount-path=/mnt/data \
  --command=python --args="-m,uvicorn,app.api:app,--host,0.0.0.0,--port,8000,--forwarded-allow-ips=*" \
  --startup-probe=httpGet.path=/health,httpGet.port=8000,periodSeconds=10,timeoutSeconds=5,failureThreshold=12 \
  --set-secrets=APP_SECRET_KEY=caa-app-secret-key:latest,CHAT_API_TOKEN=caa-chat-api-token:latest,DATABASE_URL=caa-database-url:latest \
  --set-env-vars=ADMIN_PORT=0,ADMIN_ALLOW_REMOTE=true,ADMIN_REQUIRE_HTTPS=true,ADMIN_PUBLIC_URL=$APP_URL,WEB_UI_URL=$WEB_URL,ADMIN_EMAIL_DOMAIN=sysfore.com,DOCS_DIR=/mnt/data/kb_files,CHAT_DIR=/mnt/data/chats,MODELS_DIR=/app/models,GUARDRAILS_ENABLED=true,MAX_UPLOAD_MB=25

# Streamlit web chat. Nothing runs in the background, so request-based billing is fine. Session affinity keeps each
# browser tab's live connection on the same instance.
gcloud run deploy caa-uat-web --project=$P --region=$R --image=$IMAGE \
  --service-account=caa-uat-web@$P.iam.gserviceaccount.com \
  --port=8080 --cpu=1 --memory=512Mi \
  --min-instances=0 --max-instances=1 --timeout=3600 --session-affinity \
  --allow-unauthenticated --ingress=all \
  --command=streamlit --args="run,streamlit_app.py,--server.port,8080,--server.address,0.0.0.0,--server.headless,true" \
  --set-env-vars=RAG_BACKEND_URL=$APP_URL

echo
echo "App and admin center: $APP_URL/admin"
echo "Web UI:               $WEB_URL"
