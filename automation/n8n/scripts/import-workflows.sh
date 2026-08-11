#!/usr/bin/env bash
# Render non-secret templates, then import them into a running local n8n stack.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
N8N_DIR="$ROOT/automation/n8n"
COMPOSE_FILE="$N8N_DIR/compose.yaml"
ENV_FILE="$N8N_DIR/.env"
DASHBOARD_URL=""

usage() {
  echo "Usage: import-workflows.sh --dashboard-url http[s]://HOST:PORT" >&2
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dashboard-url) DASHBOARD_URL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$DASHBOARD_URL" ]] || { usage; exit 2; }
[[ -f "$ENV_FILE" ]] || { echo "Run host-install.sh first." >&2; exit 2; }

python3 "$N8N_DIR/scripts/render_workflows.py" \
  --dashboard-url "$DASHBOARD_URL" \
  --output-dir "$N8N_DIR/state/rendered-workflows"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps n8n
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T n8n \
  n8n import:workflow \
    --separate \
    --input=/files/rendered-workflows

cat <<'EOF'
Imported inactive workflows. Before activating any Schedule Trigger workflow:
1. Attach the protected `Hermes n8n cron` HTTP Header Auth credential to both
   HTTP Request nodes in every workflow.
2. Manually run the repo-fetch-check workflow at a non-scheduled time.
3. Verify the source Hermes job reports `last_status=ok` and is paused by the
   workflow, then restore it with the documented canary restore command.
4. Run the documented cutover command first. It pauses the legacy schedules
   while the imported Schedule Trigger workflows remain inactive.
5. Only after that command succeeds, activate the Schedule Trigger workflows.

GitHub Trigger workflows remain inactive until a reviewed public HTTPS ingress
sets N8N_WEBHOOK_URL. Their activation creates signed GitHub webhooks.
EOF
