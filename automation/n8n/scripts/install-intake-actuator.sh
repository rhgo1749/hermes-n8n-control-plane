#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

CONTAINER_NAME="${HERMES_CONTAINER_NAME:-hermes-cloudcli-agent}"
HOST_UID="${HERMES_RUNTIME_UID:-1000}"
HOST_GID="${HERMES_RUNTIME_GID:-1000}"

SOURCE="$ROOT/automation/hermes/actuator/github_intake_actuator.py"

SECRET_DIR="$ROOT/automation/n8n/state/secrets"
TOKEN_HOST="$SECRET_DIR/hermes-intake-control-token"

LIBEXEC_DIR="/home/hermes/.local/libexec"
BIN_DIR="/home/hermes/.local/bin"
CONTROL_DIR="/home/hermes/.hermes/.control-plane"

ACTUATOR_REMOTE="$LIBEXEC_DIR/github_intake_actuator.py"
TOKEN_REMOTE="$CONTROL_DIR/github-intake-control-token"
LAUNCHER_REMOTE="$BIN_DIR/hermes-github-intake-actuator"

UNIT_NAME="hermes-github-intake-actuator.service"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"

TMP="$(mktemp -d /tmp/hermes-intake-actuator.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

for cmd in docker python3 systemctl install stat curl; do
    command -v "$cmd" >/dev/null 2>&1 || fail "missing command: $cmd"
done

[[ "$EUID" == "0" ]] || fail "run with sudo"
[[ -s "$SOURCE" ]] || fail "missing actuator source: $SOURCE"

docker ps --format '{{.Names}}' \
    | grep -Fxq "$CONTAINER_NAME" \
    || fail "Hermes container is not running: $CONTAINER_NAME"

[[ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$CONTAINER_NAME")" == "host" ]] \
    || fail "Hermes container must use host networking"

install -d -o "$HOST_UID" -g "$HOST_GID" -m 700 "$SECRET_DIR"

if [[ ! -f "$TOKEN_HOST" ]]; then
    umask 077
    python3 -c 'import secrets; print(secrets.token_hex(32))' \
        > "$TMP/token"
    install \
        -o "$HOST_UID" -g "$HOST_GID" -m 600 \
        "$TMP/token" "$TOKEN_HOST"
fi

chmod 600 "$TOKEN_HOST"

python3 - "$TOKEN_HOST" <<'PY'
from pathlib import Path
import re
import sys

value = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
if not re.fullmatch(r"(?:[A-Za-z0-9_-]{43}|[0-9a-f]{64})", value):
    raise SystemExit("invalid intake control token")
print("intake control token contract: PASS")
PY

mkdir -p "$TMP/pycache"
PYTHONPYCACHEPREFIX="$TMP/pycache" python3 -m py_compile "$SOURCE"

hermes_exec() {
    docker exec \
        -i \
        --user 1000:1000 \
        --env HOME=/home/hermes \
        --env HERMES_HOME=/home/hermes/.hermes \
        "$CONTAINER_NAME" \
        "$@"
}

hermes_write() {
    local source="$1"
    local destination="$2"
    local mode="$3"

    docker exec \
        -i \
        --user 1000:1000 \
        --env HOME=/home/hermes \
        "$CONTAINER_NAME" \
        sh -c '
          set -eu
          destination="$1"
          mode="$2"
          tmp="${destination}.tmp.$$"
          trap "rm -f \"$tmp\"" EXIT HUP INT TERM
          umask 077
          cat > "$tmp"
          chmod "$mode" "$tmp"
          mv -f "$tmp" "$destination"
          trap - EXIT HUP INT TERM
        ' sh "$destination" "$mode" < "$source"
}

[[ "$(hermes_exec id -u)" == "1000" ]] \
    || fail "unexpected Hermes runtime uid"

hermes_exec mkdir -p \
    "$LIBEXEC_DIR" \
    "$BIN_DIR" \
    "$CONTROL_DIR"

hermes_exec chmod 700 "$CONTROL_DIR"

hermes_write "$SOURCE" "$ACTUATOR_REMOTE" 0755
hermes_write "$TOKEN_HOST" "$TOKEN_REMOTE" 0600

cat > "$TMP/launcher" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail

export HOME=/home/hermes
export HERMES_HOME=/home/hermes/.hermes
export HERMES_INTAKE_ACTUATOR_TOKEN_FILE=/home/hermes/.hermes/.control-plane/github-intake-control-token

exec /opt/venv/bin/python3 \
  /home/hermes/.local/libexec/github_intake_actuator.py
LAUNCHER

hermes_write "$TMP/launcher" "$LAUNCHER_REMOTE" 0755

hermes_exec /opt/venv/bin/python3 -m py_compile "$ACTUATOR_REMOTE"

DOCKER_BIN="$(command -v docker)"

cat > "$TMP/unit" <<UNIT
[Unit]
Description=Hermes GitHub intake direct actuator
After=docker.service
Requires=docker.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=$DOCKER_BIN exec --user 1000:1000 --env HOME=/home/hermes --env HERMES_HOME=/home/hermes/.hermes $CONTAINER_NAME $LAUNCHER_REMOTE
Restart=always
RestartSec=5s
TimeoutStopSec=15s
KillMode=control-group
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hermes-github-intake-actuator

[Install]
WantedBy=multi-user.target
UNIT

install -o root -g root -m 0644 "$TMP/unit" "$UNIT_PATH"

systemctl daemon-reload
systemctl enable "$UNIT_NAME" >/dev/null
systemctl restart "$UNIT_NAME"

for _ in $(seq 1 30); do
    if curl \
        --fail \
        --silent \
        http://127.0.0.1:5682/healthz \
        >/dev/null 2>&1
    then
        break
    fi
    sleep .2
done

python3 - <<'PYHEALTH'
import json
import urllib.request

with urllib.request.urlopen(
    "http://127.0.0.1:5682/healthz",
    timeout=3,
) as response:
    payload = json.load(response)

assert payload["ok"] is True, payload
assert payload["service"] == "hermes-github-intake-actuator", payload
assert payload["token_ready"] is True, payload
assert payload["runtime_ready"] is True, payload

print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
PYHEALTH

echo "Installed: $UNIT_NAME"
echo "Token:     $TOKEN_HOST (not printed)"
echo "Endpoint:  http://127.0.0.1:5682/v1/intake"
echo "Edge sync: http://127.0.0.1:5682/v1/edge-sync (fixed board argv)"
