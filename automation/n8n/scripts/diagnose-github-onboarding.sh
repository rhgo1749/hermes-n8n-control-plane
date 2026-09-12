#!/usr/bin/env bash
# Diagnose the existing GitHub intake boundary without mutating runtime state.
set -Eeuo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
ROUTER_URL="${GITHUB_ROUTER_URL:-http://127.0.0.1:5681}"
LEASE_URL="${GITHUB_LEASE_URL:-http://127.0.0.1:5680}"
ACTUATOR_URL="${GITHUB_INTAKE_ACTUATOR_URL:-http://127.0.0.1:5682}"
SKIP_NETWORK=0

usage() {
  printf '%s\n' \
    'Usage: diagnose-github-onboarding.sh [--hermes-home PATH] [--skip-network]' \
    '' \
    'Checks the deployed intake runtime and loopback service health.' \
    'This command never creates, edits, pauses, or triggers intake work.'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      HERMES_HOME="$2"
      shift 2
      ;;
    --skip-network)
      SKIP_NETWORK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! runtime_path_result="$(python3 - "$HERMES_HOME/scripts" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
if not path.is_absolute():
    print("authoritative_intake_runtime_invalid reason=path_not_absolute", file=sys.stderr)
    raise SystemExit(1)

current = Path(path.anchor)
for part in path.parts[1:]:
    current /= part
    if current.is_symlink():
        print(
            f"authoritative_intake_runtime_invalid reason=path_symlink path={current}",
            file=sys.stderr,
        )
        raise SystemExit(1)
PY
)"; then
  printf '%s\n' "$runtime_path_result" >&2
  exit 1
fi

declare -A intake_script_contracts=(
  ["$HERMES_HOME/scripts/github-agent-ready-kanban-intake.py"]="_core_path,_load_core,_install_completion_contract_overlay,main"
  ["$HERMES_HOME/scripts/github-agent-ready-kanban-intake-core.py"]="_onboarding_repository_metadata,_ensure_checkout,_validate_onboarding_checkout,_materialize_onboarding_checkout,_provision_scoped_checkouts,_scope_transition,_task_body,main"
)
for intake_script in "${!intake_script_contracts[@]}"; do
  if [[ ! -f "$intake_script" || -L "$intake_script" ]]; then
    printf 'authoritative_intake_runtime_missing path=%s\n' "$intake_script" >&2
    exit 1
  fi
  if ! python3 - "$intake_script" "${intake_script_contracts[$intake_script]}" <<'PY'
import ast
import sys
from pathlib import Path

path = Path(sys.argv[1])
required = tuple(item for item in sys.argv[2].split(",") if item)
max_script_bytes = 4 * 1024 * 1024
try:
    source = path.read_bytes()
    if len(source) > max_script_bytes:
        raise ValueError("oversized")
    tree = ast.parse(source.decode("utf-8"), filename="intake-script")
except (OSError, UnicodeError, SyntaxError, ValueError):
    raise SystemExit(1)
functions = {
    node.name
    for node in tree.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}
if not set(required).issubset(functions):
    raise SystemExit(1)
PY
  then
    printf 'authoritative_intake_runtime_invalid path=%s\n' "$intake_script" >&2
    exit 1
  fi
done
printf '%s\n' 'intake_execution=direct-actuator:5682 hermes_cron_required=false'
printf '%s\n' 'intake_scripts_present=true contract=verified'

if (( SKIP_NETWORK )); then
  printf '%s\n' 'network_probes=skipped'
  exit 0
fi

is_loopback_url() {
  local url="$1"
  [[ "$url" == http://127.0.0.1:* || "$url" == http://localhost:* || "$url" == http://\[::1\]:* ]] \
    && [[ "$url" != *\?* && "$url" != *\&* && "$url" != *\#* && "$url" != *@* ]]
}

network_failed=0
for probe in \
  "router:$ROUTER_URL/healthz" \
  "lease-controller:$LEASE_URL/healthz"; do
  service="${probe%%:*}"
  url="${probe#*:}"
  if ! is_loopback_url "$url"; then
    printf '%s_health=invalid_url\n' "$service" >&2
    network_failed=1
    continue
  fi
  health_body=""
  if health_body="$(curl -q --fail --silent --show-error --max-time 3 \
    --max-filesize 65536 "$url")" \
    && python3 -c 'import json, sys; value = json.load(sys.stdin); raise SystemExit(value.get("ok") is not True)' <<<"$health_body"; then
    printf '%s_health=ok\n' "$service"
  else
    printf '%s_health=unavailable_or_invalid\n' "$service" >&2
    network_failed=1
  fi
done

actuator_health_url="$ACTUATOR_URL/healthz"
if ! is_loopback_url "$actuator_health_url"; then
  printf '%s\n' 'intake-actuator_health=invalid_url' >&2
  network_failed=1
else
  actuator_health=""
  if actuator_health="$(curl -q --fail --silent --show-error --max-time 3 \
    --max-filesize 65536 "$actuator_health_url")" \
    && python3 -c '
import json, sys
value = json.load(sys.stdin)
required = {
    "ok": True,
    "service": "hermes-github-intake-actuator",
    "token_ready": True,
    "runtime_ready": True,
    "edge_sync_runtime_ready": True,
}
raise SystemExit(any(value.get(key) != expected for key, expected in required.items()))
' <<<"$actuator_health"; then
    printf '%s\n' 'intake-actuator_health=ok runtime_ready=true edge_sync_runtime_ready=true'
  else
    printf '%s\n' 'intake-actuator_health=unavailable_or_invalid' >&2
    network_failed=1
  fi
fi

trigger_url="$LEASE_URL/trigger"
trigger_status=""
if ! is_loopback_url "$trigger_url"; then
  printf '%s\n' 'lease_trigger_contract=unavailable status=invalid_url' >&2
  network_failed=1
elif trigger_headers="$(curl -q --silent --show-error --max-time 3 \
  --output /dev/null --dump-header - --request OPTIONS "$trigger_url")"; then
  trigger_status="$(printf '%s' "$trigger_headers" | awk 'toupper($1) ~ /^HTTP\// { status=$2 } END { print status }')"
  allow_header="$(printf '%s' "$trigger_headers" | awk 'tolower($1) == "allow:" { sub(/^[^:]*:[[:space:]]*/, ""); print; exit }')"
  if [[ "$trigger_status" == "204" && ",${allow_header}," == *,POST,* ]]; then
    printf '%s\n' 'lease_trigger_contract=available method=POST execution=direct-actuator'
  else
    printf 'lease_trigger_contract=unavailable status=%s allow=%s\n' "$trigger_status" "$allow_header" >&2
    network_failed=1
  fi
else
  printf '%s\n' 'lease_trigger_contract=unavailable status=000' >&2
  network_failed=1
fi

if (( network_failed )); then
  exit 1
fi
printf '%s\n' 'network_probes=ok'
