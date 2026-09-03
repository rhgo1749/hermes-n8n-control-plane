#!/usr/bin/env bash
# Emergency bounded recovery for Issue #106.
# Run on the Ubuntu host from this repository checkout.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
N8N_DIR="$ROOT/automation/n8n"
# shellcheck source=state-root.sh
. "$SCRIPT_DIR/state-root.sh"
ENV_FILE="$(h4v3_n8n_env_file "$N8N_DIR")"
STATE_ROOT="$(h4v3_n8n_state_root "$N8N_DIR")"
COMPOSE_FILE="$N8N_DIR/compose.yaml"
CONTAINER_NAME="${HERMES_CONTAINER_NAME:-hermes-cloudcli-agent}"
REPOSITORY="rhgo1749/ctrl-hangul"
RESTORE_FUNNEL=1
TRIGGER_EDGE=1

usage() {
  cat <<'EOF'
Usage: sudo automation/n8n/scripts/recover-edge-dispatch-outage.sh [options]

Options:
  --repository OWNER/REPO   Pending rework repository to reconcile
                            (default: rhgo1749/ctrl-hangul)
  --skip-funnel             Do not rewrite the canonical Tailscale :10000 rule
  --skip-trigger            Restore services only; do not invoke /v1/edge-sync
  -h, --help                Show help

This is a bounded host recovery. It does not edit Hermes core source, Kanban
job definitions, PR branches, or merge state.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repository)
      [[ $# -ge 2 ]] || { echo "--repository requires OWNER/REPO" >&2; exit 2; }
      REPOSITORY="$2"
      shift 2
      ;;
    --skip-funnel) RESTORE_FUNNEL=0; shift ;;
    --skip-trigger) TRIGGER_EDGE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ "$EUID" == 0 ]] || fail "run with sudo"
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
  || fail "invalid repository: $REPOSITORY"

for cmd in docker curl python3 systemctl; do
  command -v "$cmd" >/dev/null 2>&1 || fail "missing command: $cmd"
done

[[ -f "$ENV_FILE" ]] || fail "runtime env missing: $ENV_FILE"
[[ -f "$COMPOSE_FILE" ]] || fail "compose file missing: $COMPOSE_FILE"

export HERMES_N8N_ENV_FILE="$ENV_FILE"
export HERMES_N8N_STATE_ROOT="$STATE_ROOT"

start_existing_compose_service() {
  local service="$1"
  local ids
  ids="$(docker ps -aq \
    --filter 'label=com.docker.compose.project=hermes-n8n-control-plane' \
    --filter "label=com.docker.compose.service=$service")"
  [[ -n "$ids" ]] || fail "no existing Compose container for service: $service"
  # shellcheck disable=SC2086
  docker start $ids >/dev/null
}

echo "[1/5] restore private n8n/router/lease stack"
systemctl enable --now docker >/dev/null
if ! docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d; then
  echo "WARN: Compose reconciliation is blocked by runtime env drift; starting the existing reviewed containers in-place." >&2
  echo "WARN: repair the missing Compose env contract after service recovery; existing container configuration is preserved for this bounded start." >&2
  start_existing_compose_service n8n
  start_existing_compose_service lease-controller
  start_existing_compose_service github-router
fi

for endpoint in \
  http://127.0.0.1:5678/healthz \
  http://127.0.0.1:5680/healthz \
  http://127.0.0.1:5681/healthz
do
  ok=0
  for _ in $(seq 1 30); do
    if curl --fail --silent --show-error "$endpoint" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 1
  done
  [[ "$ok" == 1 ]] || fail "service did not become healthy: $endpoint"
done

echo "[2/5] restore canonical public intake Funnel"
if [[ "$RESTORE_FUNNEL" == 1 ]]; then
  command -v tailscale >/dev/null 2>&1 || fail "tailscale CLI missing"
  # This endpoint must be reachable from public GitHub. `tailscale serve` is
  # tailnet-only on current clients; the most recent command for a port decides
  # whether that port is Serve or Funnel. Reassert an explicit persistent
  # Funnel for only the intake path.
  tailscale funnel --bg --yes --https=10000 \
    --set-path=/github/hermes-intake \
    http://127.0.0.1:5681/github/hermes-intake
  tailscale funnel status
else
  echo "skip: Funnel recovery"
fi

echo "[3/5] reinstall actuator with canonical Hermes source path"
"$SCRIPT_DIR/install-intake-actuator.sh"

curl --fail --silent --show-error http://127.0.0.1:5682/healthz \
  | python3 -c '
import json, sys
p=json.load(sys.stdin)
assert p.get("ok") is True, p
assert p.get("token_ready") is True, p
assert p.get("runtime_ready") is True, p
assert p.get("edge_sync_runtime_ready") is True, p
print(json.dumps(p, ensure_ascii=False, sort_keys=True))
'

echo "[4/5] repair current kanban-main fallback script lookup without editing job metadata"
docker exec \
  --user 1000:1000 \
  --env HOME=/home/hermes \
  --env HERMES_HOME=/home/hermes/.hermes \
  "$CONTAINER_NAME" \
  sh -c '
    set -eu
    src=/home/hermes/.hermes/scripts/kanban-github-edge-fallback-sync.py
    dst_dir=/home/hermes/.hermes/profiles/kanban-main/scripts
    dst=$dst_dir/kanban-github-edge-fallback-sync.py
    if [ -f "$src" ]; then
      mkdir -p "$dst_dir"
      tmp="$dst.tmp.$$"
      cp -p "$src" "$tmp"
      mv -f "$tmp" "$dst"
      echo "fallback mirror repaired: $dst"
    else
      echo "WARN: canonical fallback source is absent: $src" >&2
    fi
  '

if [[ "$TRIGGER_EDGE" == 1 ]]; then
  echo "[5/5] consume pending fresh agent-rework through the canonical actuator"
  TOKEN_FILE="$STATE_ROOT/secrets/hermes-intake-control-token"
  [[ -s "$TOKEN_FILE" ]] || fail "actuator token missing: $TOKEN_FILE"
  python3 - "$TOKEN_FILE" "$REPOSITORY" <<'PY'
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

token = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
repository = sys.argv[2]
payload = {
    "repository": repository,
    "event": "pull_request",
    "action": "labeled",
    "merged": False,
    "label": "agent-rework",
    "delivery": f"operator-recovery-{int(time.time())}",
}
request = Request(
    "http://127.0.0.1:5682/v1/edge-sync",
    method="POST",
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    },
    data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
)
with urlopen(request, timeout=180) as response:
    body = json.load(response)
    if response.status != 200 or body.get("ok") is not True:
        raise SystemExit(f"edge recovery failed: HTTP {response.status} {body!r}")
    print(json.dumps(body, ensure_ascii=False, sort_keys=True))
PY
else
  echo "[5/5] skip: edge trigger"
fi

echo "Recovery completed. Re-read GitHub PR labels and Kanban state before any further mutation."
