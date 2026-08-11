#!/usr/bin/env bash
# Export workflow state for review. Credentials are not exported, but workflow
# exports can retain credential names/IDs, so this always writes to ignored state.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
N8N_DIR="$ROOT/automation/n8n"
COMPOSE_FILE="$N8N_DIR/compose.yaml"
ENV_FILE="$N8N_DIR/.env"
[[ -f "$ENV_FILE" ]] || { echo "Run host-install.sh first." >&2; exit 2; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$N8N_DIR/state/exports"
output="/files/exports/workflows-$stamp.json"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" exec -T n8n \
  n8n export:workflow --all --output="$output"

echo "Export written to $N8N_DIR/state/exports/workflows-$stamp.json"
echo "Review and remove credential names/IDs before copying any changed workflow JSON into Git."
