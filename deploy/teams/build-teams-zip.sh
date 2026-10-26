#!/usr/bin/env bash
# Builds the Teams app package to upload in the Teams admin center (Manage apps > Upload new app).
#   bash deploy/teams/build-teams-zip.sh <azure-bot-app-id> [app-host]
# The App ID is the Azure Bot's "Microsoft App ID". The host defaults to the UAT Cloud Run address.
set -euo pipefail

APP_ID=${1:?usage: build-teams-zip.sh <azure-bot-app-id> [app-host]}
APP_HOST=${2:-caa-uat-app-251420217705.asia-south1.run.app}
[[ $APP_ID =~ ^[0-9a-fA-F-]{36}$ ]] || { echo "The App ID should be a GUID, e.g. 00000000-0000-0000-0000-000000000000" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

sed -e "s/{{TEAMS_APP_ID}}/$APP_ID/" -e "s/{{APP_HOST}}/$APP_HOST/" "$HERE/manifest.json" > "$WORK/manifest.json"
cp "$HERE/color.png" "$HERE/outline.png" "$WORK/"

# The three files must sit at the root of the zip. Python's zipfile is used because Windows has no zip command.
OUT="$HERE/caa-uat-teams.zip"
python - "$WORK" "$OUT" <<'EOF'
import sys, zipfile, pathlib
src, out = pathlib.Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name in ("manifest.json", "color.png", "outline.png"):
        z.write(src / name, name)
EOF
echo "Built $OUT"
