#!/usr/bin/env bash
# Render and import the single hourly GitHub intake fallback workflow.
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

python3 "$N8N_DIR/scripts/render_workflows.py" \
  --dashboard-url "$DASHBOARD_URL" \
  --output-dir "$RENDERED_DIR"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps n8n
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T n8n \
  n8n import:workflow \
    --separate \
    --input=/files/rendered-workflows

cat <<'EOF'
Imported the inactive hourly fallback workflow:
  Hermes fallback · GitHub Kanban intake

Primary path remains event-driven:
  GitHub webhook -> github-router -> lease-controller -> existing Hermes job
  default:bf431b2a6ba6

Fallback path runs once per hour and calls github-router /fallback. It performs
webhook reconciliation plus a full-registry intake wake so missed GitHub events
or transient delivery failures are eventually recovered.

Attach the protected Hermes n8n cron HTTP Header Auth credential to the
"Run registry fallback" node, verify one manual execution, then activate only
this hourly fallback workflow.

The Hermes job itself remains preserved and should normally stay paused between
external trigger/pause leases. Do not delete or recreate default:bf431b2a6ba6.
EOF
