#!/usr/bin/env bash
# Export workflow state for review. Credentials are not exported, but workflow
# exports can retain credential names/IDs, so this always writes to private state.
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
[[ -f "$ENV_FILE" ]] || { echo "Run host-install.sh first." >&2; exit 2; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$STATE_ROOT/exports"
output="/files/exports/workflows-$stamp.json"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T n8n \
  n8n export:workflow --all --output="$output"

echo "Export written to $STATE_ROOT/exports/workflows-$stamp.json"
echo "Review and remove credential names/IDs before copying any changed workflow JSON into Git."