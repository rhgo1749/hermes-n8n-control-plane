#!/usr/bin/env bash
# Render, import, publish, and canary the managed GitHub edge-sync workflow.
# Run this on the Ubuntu HOST, not inside the Hermes worker container.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
N8N_DIR="$ROOT/automation/n8n"
COMPOSE_FILE="$N8N_DIR/compose.yaml"
# shellcheck source=state-root.sh
# shellcheck disable=SC1091
. "$SCRIPT_DIR/state-root.sh"
ENV_FILE="$(h4v3_n8n_env_file "$N8N_DIR")"
export HERMES_N8N_ENV_FILE="$ENV_FILE"
STATE_ROOT="$(h4v3_n8n_state_root "$N8N_DIR")"
export HERMES_N8N_STATE_ROOT="$STATE_ROOT"

TOKEN_FILE="$STATE_ROOT/secrets/hermes-intake-control-token"
ACTUATOR_HEALTH_URL="http://127.0.0.1:5682/healthz"
N8N_HEALTH_URL="http://127.0.0.1:5678/healthz"
EDGE_SYNC_WEBHOOK_URL="http://127.0.0.1:5678/webhook/hermes-github-edge-sync"
RENDERED_DIR="$STATE_ROOT/rendered-workflows"
RUNTIME_DIR=""

usage() {
  echo "Usage: import-workflows.sh" >&2
}

fail() {
  echo "import-workflows: ERROR: $*" >&2
  exit 1
}

cleanup() {
  if [[ -n "$RUNTIME_DIR" && -d "$RUNTIME_DIR" ]]; then
    rm -rf -- "$RUNTIME_DIR"
  fi
}
trap cleanup EXIT

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

[[ -f "$ENV_FILE" ]] || fail "run host-install.sh first"
[[ -f "$TOKEN_FILE" ]] || fail "required control-token file is missing"
[[ -r "$TOKEN_FILE" ]] || fail "required control-token file is not readable"
command -v docker >/dev/null 2>&1 || fail "Docker is required"
command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"

# Check only metadata and validity constraints here; never print the token.
if ! PYTHONDONTWRITEBYTECODE=1 python3 - "$TOKEN_FILE" <<'PY'
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
info = path.stat()
if not stat.S_ISREG(info.st_mode):
    raise SystemExit(1)
if stat.S_IMODE(info.st_mode) & 0o077:
    raise SystemExit(1)
if info.st_size <= 0 or info.st_size > 256:
    raise SystemExit(1)
value = path.read_text(encoding="utf-8").strip()
if not value or any(character.isspace() for character in value):
    raise SystemExit(1)
PY
then
  fail "required control-token file is invalid or has unsafe permissions"
fi

compose=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")
n8n_cli() {
  "${compose[@]}" exec -T n8n n8n "$@"
}

wait_for_n8n_health() {
  local attempts="${1:-30}"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt += 1)); do
    if curl --fail --silent --show-error --max-time 5 "$N8N_HEALTH_URL" >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

CANARY_HTTP_STATUS="000"

run_canary_once() {
  local response_file="$RUNTIME_DIR/canary-response.json"
  local http_status

  if ! http_status="$(curl --silent --show-error --max-time 30 \
    --config "$CURL_CONFIG" \
    -X POST \
    -H "Content-Type: application/json" \
    --data-binary "$CANARY_PAYLOAD" \
    -o "$response_file" \
    -w '%{http_code}' \
    "$EDGE_SYNC_WEBHOOK_URL")"; then
    CANARY_HTTP_STATUS="000"
    return 1
  fi
  CANARY_HTTP_STATUS="$http_status"
  [[ "$http_status" =~ ^2[0-9][0-9]$ ]] || return 1

  python3 - "$response_file" <<'PY2'
import json
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
items = value if isinstance(value, list) else [value]


def mappings(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from mappings(child)


if not any(
    item.get("ignored") is True
    and item.get("reason") == "unsupported_pull_request_action"
    for value in items
    for item in mappings(value)
):
    raise SystemExit(1)
PY2
}

wait_for_canary() {
  local attempts="${1:-30}"
  local attempt
  for ((attempt = 1; attempt <= attempts; attempt += 1)); do
    if run_canary_once; then
      return 0
    fi
    sleep 2
  done
  return 1
}

preflight_actuator() {
  local response
  if ! response="$(curl --fail --silent --show-error --max-time 5 "$ACTUATOR_HEALTH_URL")"; then
    return 1
  fi
  printf '%s' "$response" | python3 -c '
import json
import sys

value = json.load(sys.stdin)
raise SystemExit(0 if isinstance(value, dict) and value.get("edge_sync_runtime_ready") is True else 1)
'
}

if ! preflight_actuator; then
  fail "actuator health must report edge_sync_runtime_ready=true before deployment"
fi
if ! wait_for_n8n_health 30; then
  fail "n8n did not become healthy before deployment"
fi

# These are the only server-CLI commands this deployment path relies on. A
# missing command is a hard failure, never a partially deployed workflow.
for command in import:credentials import:workflow publish:workflow export:workflow; do
  if ! n8n_cli "$command" --help >/dev/null 2>&1; then
    fail "required n8n server CLI command is unavailable: $command"
  fi
done

mkdir -p "$RENDERED_DIR"
rm -f "$RENDERED_DIR"/schedule-*.json "$RENDERED_DIR"/github-*.json
PYTHONDONTWRITEBYTECODE=1 python3 "$N8N_DIR/scripts/render_workflows.py" \
  --output-dir "$RENDERED_DIR"
TEMPLATE="$RENDERED_DIR/github-pr-edge-sync.json"
[[ -f "$TEMPLATE" ]] || fail "rendered edge-sync workflow is missing"

RUNTIME_DIR="$(mktemp -d "$STATE_ROOT/.edge-sync-runtime.XXXXXX")"
chmod 700 "$RUNTIME_DIR"
case "$RUNTIME_DIR" in
  "$STATE_ROOT"/*) ;;
  *) fail "runtime temporary directory escaped the configured state root" ;;
esac
RUNTIME_REL="${RUNTIME_DIR#"$STATE_ROOT"/}"
INVENTORY_JSON="$RUNTIME_DIR/workflows-all.json"
INVENTORY_CONTAINER_PATH="/files/$RUNTIME_REL/workflows-all.json"
CREDENTIAL_JSON="$RUNTIME_DIR/credential.json"
WORKFLOW_JSON="$RUNTIME_DIR/workflow.json"
CURL_CONFIG="$RUNTIME_DIR/canary.curlrc"
PUBLISHED_PROBE_CONTAINER_PATH="/files/$RUNTIME_REL/published-before-import.json"

# Inventory the server before preparing or importing any replacement. The
# export stays in the private runtime directory and is never printed.
n8n_cli export:workflow \
  --all \
  --output="$INVENTORY_CONTAINER_PATH"
[[ -f "$INVENTORY_JSON" ]] || fail "n8n workflow inventory export is missing"

MANAGED_WORKFLOW_ID="$(
  PYTHONDONTWRITEBYTECODE=1 python3 - "$INVENTORY_JSON" "$N8N_DIR/scripts" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[2])
from prepare_edge_sync_runtime import select_managed_workflow_id  # noqa: E402

try:
    inventory = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(select_managed_workflow_id(inventory))
except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
    print(f"import-workflows: ERROR: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
)" || fail "managed workflow inventory selection failed; no workflow was imported"

# A previously published legacy record can contain an unsupported node. n8n's
# import command tries to deactivate that old graph before replacing it and may
# crash while clearing its webhooks. Unpublish only the exact managed record
# first when a published version exists; this avoids touching unrelated flows
# and lets the replacement import start from an inactive record.
if n8n_cli export:workflow \
  --id="$MANAGED_WORKFLOW_ID" \
  --published \
  --output="$PUBLISHED_PROBE_CONTAINER_PATH" >/dev/null 2>&1; then
  if ! n8n_cli unpublish:workflow --help >/dev/null 2>&1; then
    fail "managed workflow is published but n8n unpublish command is unavailable"
  fi
  n8n_cli unpublish:workflow --id="$MANAGED_WORKFLOW_ID"
fi

PYTHONDONTWRITEBYTECODE=1 python3 "$N8N_DIR/scripts/prepare_edge_sync_runtime.py" \
  --token-file "$TOKEN_FILE" \
  --workflow-template "$TEMPLATE" \
  --credential-output "$CREDENTIAL_JSON" \
  --workflow-output "$WORKFLOW_JSON" \
  --workflow-id "$MANAGED_WORKFLOW_ID" \
  --curl-config-output "$CURL_CONFIG"

CREDENTIAL_CONTAINER_PATH="/files/$RUNTIME_REL/credential.json"
WORKFLOW_CONTAINER_PATH="/files/$RUNTIME_REL/workflow.json"
VERIFY_JSON="$RUNTIME_DIR/verified-workflow.json"
VERIFY_CONTAINER_PATH="/files/$RUNTIME_REL/verified-workflow.json"
RUNTIME_WORKFLOW_ID="$(python3 - "$WORKFLOW_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
workflow_id = value.get("id") if isinstance(value, dict) else None
if not isinstance(workflow_id, str) or not workflow_id:
    raise SystemExit(1)
print(workflow_id)
PY
)" || fail "runtime workflow has no managed ID"
[[ "$RUNTIME_WORKFLOW_ID" == "$MANAGED_WORKFLOW_ID" ]] || fail "runtime workflow ID changed during preparation"

# The credential is imported first so both workflow references resolve during
# the workflow import. The selected managed ID makes this command idempotent,
# and the single-file imports cannot delete unrelated workflows.
n8n_cli import:credentials --input="$CREDENTIAL_CONTAINER_PATH"
n8n_cli import:workflow \
  --input="$WORKFLOW_CONTAINER_PATH" \
  --activeState=false
n8n_cli publish:workflow --id="$MANAGED_WORKFLOW_ID"

# n8n 2.x persists publication in the database but requires a restart before
# production workers use the new published version.
"${compose[@]}" restart n8n
if ! wait_for_n8n_health 30; then
  fail "n8n did not become healthy after publishing the workflow"
fi

# Read back the published workflow through the server CLI and verify the exact
# managed credential binding survived import/publication on both nodes.
n8n_cli export:workflow \
  --id="$MANAGED_WORKFLOW_ID" \
  --published \
  --output="$VERIFY_CONTAINER_PATH"
python3 - "$VERIFY_JSON" "$WORKFLOW_JSON" <<'PY'
import json
import sys
from pathlib import Path

export_path = Path(sys.argv[1])
runtime_path = Path(sys.argv[2])
exported = json.loads(export_path.read_text(encoding="utf-8"))
runtime = json.loads(runtime_path.read_text(encoding="utf-8"))

if isinstance(exported, list):
    candidates = [item for item in exported if isinstance(item, dict)]
else:
    candidates = [exported] if isinstance(exported, dict) else []
workflow_id = runtime.get("id")
expected = None
for candidate in candidates:
    if candidate.get("id") == workflow_id:
        expected = candidate
        break
if expected is None:
    raise SystemExit(1)

expected_ref = None
for node in runtime.get("nodes", []):
    if node.get("name") == "GitHub edge sync webhook":
        expected_ref = node.get("credentials", {}).get("httpHeaderAuth")
        break
if not isinstance(expected_ref, dict):
    raise SystemExit(1)

for name in ("GitHub edge sync webhook", "Run edge sync actuator"):
    matches = [node for node in expected.get("nodes", []) if node.get("name") == name]
    if len(matches) != 1:
        raise SystemExit(1)
    actual = matches[0].get("credentials", {}).get("httpHeaderAuth")
    if actual != expected_ref:
        raise SystemExit(1)
PY

CANARY_PAYLOAD='{"repository":"example/unsupported-canary","event":"pull_request","action":"opened","merged":false,"label":"","delivery":"hermes-n8n-deploy-canary"}'
if ! wait_for_canary 30; then
  if [[ "$CANARY_HTTP_STATUS" != "404" ]]; then
    fail "production edge-sync Webhook canary failed (HTTP $CANARY_HTTP_STATUS)"
  fi

  # n8n 2.32.x can retain an imported workflow's inactive runtime registry
  # after publish/restart, especially when the record previously contained an
  # unsupported node. Re-publish only this managed workflow so the registry is
  # rebuilt without touching unrelated workflows.
  if ! n8n_cli unpublish:workflow --help >/dev/null 2>&1; then
    fail "production edge-sync Webhook remained unregistered and n8n unpublish command is unavailable"
  fi
  n8n_cli unpublish:workflow --id="$MANAGED_WORKFLOW_ID"
  n8n_cli publish:workflow --id="$MANAGED_WORKFLOW_ID"
  "${compose[@]}" restart n8n
  if ! wait_for_n8n_health 30; then
    fail "n8n did not become healthy after republish fallback"
  fi
  if ! wait_for_canary 30; then
    fail "production edge-sync Webhook canary remained unregistered after republish fallback (HTTP $CANARY_HTTP_STATUS)"
  fi
fi

echo "Published managed GitHub PR edge-sync workflow with runtime credential binding."
echo "n8n is healthy and the safe unsupported-action production canary passed."
echo "Live signed GitHub delivery/redelivery remains the host-runtime evidence gate."
echo "The direct-actuator intake boundary was not modified by this workflow import."
