#!/usr/bin/env bash
# Render and import the single on-demand GitHub edge-sync workflow.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
N8N_DIR="$ROOT/automation/n8n"
COMPOSE_FILE="$N8N_DIR/compose.yaml"
# shellcheck source=state-root.sh
. "$SCRIPT_DIR/state-root.sh"
ENV_FILE="$(h4v3_n8n_env_file "$N8N_DIR")"
export HERMES_N8N_ENV_FILE="$ENV_FILE"
STATE_ROOT="$(h4v3_n8n_state_root "$N8N_DIR")"
export HERMES_N8N_STATE_ROOT="$STATE_ROOT"
usage() {
  echo "Usage: import-workflows.sh" >&2
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dashboard-url)
      echo "--dashboard-url is deprecated and ignored; edge sync uses fixed loopback endpoints." >&2
      [[ $# -ge 2 ]] || { usage; exit 2; }
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -f "$ENV_FILE" ]] || { echo "Run host-install.sh first." >&2; exit 2; }

RENDERED_DIR="$STATE_ROOT/rendered-workflows"
mkdir -p "$RENDERED_DIR"
rm -f "$RENDERED_DIR"/schedule-*.json "$RENDERED_DIR"/github-*.json

python3 "$N8N_DIR/scripts/render_workflows.py" \
  --output-dir "$RENDERED_DIR"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps n8n
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T n8n \
  n8n import:workflow \
    --separate \
    --input=/files/rendered-workflows

cat <<'EOF'
Imported the inactive on-demand Webhook workflow:
  Hermes Webhook · GitHub PR edge sync

Primary path remains event-driven:
  GitHub webhook -> github-router (HMAC/delivery/repository authority)
  -> n8n private Webhook -> fixed edge-sync actuator -> Kanban edge state

Issue and non-PR intake events continue through the existing router /
lease-controller -> Hermes intake path. The tracked n8n workflow has no
Schedule Trigger and never calls /fallback.

After import, bind the protected loopback control-token Header Auth credential
to both the Webhook trigger and "Run edge sync actuator" node. Verify one
signed host canary, then activate only this workflow.

The Hermes job itself remains preserved and should normally stay paused between
external trigger/pause leases. Do not delete or recreate default:bf431b2a6ba6.
EOF
