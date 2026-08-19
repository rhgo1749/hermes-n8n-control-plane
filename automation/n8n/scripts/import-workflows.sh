#!/usr/bin/env bash
# Async-only intake owns no n8n workflow templates.  Keep this command as a
# compatibility/status surface for older host runbooks without recreating the
# retired five-minute Schedule Trigger workflow.
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

RENDERED_DIR="$N8N_DIR/state/rendered-workflows"
mkdir -p "$RENDERED_DIR"
rm -f "$RENDERED_DIR"/schedule-*.json "$RENDERED_DIR"/github-*-intake.json

# Retain the strict legacy dashboard URL validation, but the expected rendered
# workflow set is now empty.
python3 "$N8N_DIR/scripts/render_workflows.py" \
  --dashboard-url "$DASHBOARD_URL" \
  --output-dir "$RENDERED_DIR"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps n8n

cat <<'EOF'
No n8n intake workflows were imported.

The GitHub intake is event-driven:
  GitHub webhook -> github-router -> lease-controller -> existing Hermes job
  default:bf431b2a6ba6

The Hermes job itself must be preserved and normally remain paused between
external trigger/pause leases. Do not delete, recreate, rename, or edit its
stored schedule as part of this migration.

If an older persisted n8n workflow named
`Hermes schedule · GitHub agent-ready Issue intake` still exists, keep it
inactive or delete that n8n workflow record in the n8n UI so it cannot recreate
five-minute polling.
EOF
