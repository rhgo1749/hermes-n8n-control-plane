#!/usr/bin/env bash
# Bootstrap the private, persistent n8n Community Edition Compose stack.
# Run this on the Ubuntu HOST, not inside the Hermes container.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
N8N_DIR="$ROOT/automation/n8n"
COMPOSE_FILE="$N8N_DIR/compose.yaml"
ENV_FILE="$N8N_DIR/.env"
PORT=5678
TIMEZONE="Asia/Seoul"
ENABLE_DOCKER_SERVICE=0
PORT_EXPLICIT=0
TIMEZONE_EXPLICIT=0

usage() {
  cat <<'EOF'
Usage: host-install.sh [--port 5678] [--timezone Asia/Seoul] [--enable-docker-service]

Starts n8n only on 127.0.0.1. It never exposes a public webhook endpoint,
installs Docker, mounts the Docker socket, or changes Hermes.

--enable-docker-service explicitly runs `sudo systemctl enable --now docker`
so Docker restores the `restart: unless-stopped` n8n container after host boot.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; PORT_EXPLICIT=1; shift 2 ;;
    --timezone) TIMEZONE="$2"; TIMEZONE_EXPLICIT=1; shift 2 ;;
    --enable-docker-service) ENABLE_DOCKER_SERVICE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || ! (( PORT > 0 && PORT < 65536 )); then
  echo "--port must be 1..65535" >&2
  exit 2
fi
[[ -f "$COMPOSE_FILE" && -f "$N8N_DIR/.env.example" ]] || {
  echo "n8n deployment files are missing under $N8N_DIR" >&2; exit 1;
}
command -v docker >/dev/null || {
  echo "Docker Engine is required. Install Docker Engine + Compose v2 from Docker's official Ubuntu instructions, then re-run this script." >&2
  exit 2
}
docker compose version >/dev/null || {
  echo "Docker Compose v2 is required (\`docker compose version\` failed)." >&2; exit 2;
}
if [[ "$ENABLE_DOCKER_SERVICE" == 1 ]]; then
  command -v systemctl >/dev/null || {
    echo "systemctl is unavailable; configure your host's Docker supervisor before continuing." >&2; exit 2;
  }
  sudo systemctl enable --now docker
fi
# Do not silently use sudo for every Docker command: the caller must have
# intentionally configured Docker access or re-run under an appropriate account.
docker info >/dev/null || {
  echo "Docker is installed but this user cannot access it. Configure Docker access, then re-run." >&2; exit 2;
}

install -d -m 700 "$N8N_DIR/state" "$N8N_DIR/state/rendered-workflows" \
  "$N8N_DIR/state/exports" "$N8N_DIR/state/backups"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$N8N_DIR/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# Preserve operator-provided values while atomically filling safe private
# defaults and a stable credential-encryption key. Explicit CLI values override
# the corresponding bootstrap values, including values pre-populated by the
# checked-in .env.example.
EFFECTIVE_PORT="$(python3 - "$ENV_FILE" "$PORT" "$TIMEZONE" "$PORT_EXPLICIT" "$TIMEZONE_EXPLICIT" <<'PY'
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
port, timezone, port_explicit, timezone_explicit = sys.argv[2:]
lines = path.read_text(encoding="utf-8").splitlines()
values: dict[str, str] = {}
order: list[str] = []
for line in lines:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if key:
        values[key] = value
        order.append(key)
defaults = {
    "N8N_IMAGE": "docker.n8n.io/n8nio/n8n:2.32.7",
    "N8N_PORT": "5678",
    "N8N_HOST_PORT": port,
    "N8N_HOST": "localhost",
    "N8N_PROTOCOL": "http",
    "N8N_EDITOR_BASE_URL": f"http://127.0.0.1:{port}",
    "N8N_WEBHOOK_URL": "",
    "N8N_PROXY_HOPS": "0",
    "N8N_SECURE_COOKIE": "false",
    "GENERIC_TIMEZONE": timezone,
    "TZ": timezone,
}
for key, value in defaults.items():
    if not values.get(key, "").strip():
        values[key] = value
if port_explicit == "1":
    values["N8N_HOST_PORT"] = port
    editor_url = values.get("N8N_EDITOR_BASE_URL", "").strip()
    if not editor_url or re.fullmatch(r"http://(?:127\.0\.0\.1|localhost)(?::\d+)?", editor_url):
        values["N8N_EDITOR_BASE_URL"] = f"http://127.0.0.1:{port}"
if timezone_explicit == "1":
    values["GENERIC_TIMEZONE"] = timezone
    values["TZ"] = timezone
effective_port = values["N8N_HOST_PORT"].strip()
if not effective_port.isdecimal() or not 0 < int(effective_port) < 65536:
    raise SystemExit("N8N_HOST_PORT in .env must be 1..65535")
if not values.get("N8N_ENCRYPTION_KEY", "").strip():
    values["N8N_ENCRYPTION_KEY"] = secrets.token_urlsafe(32)
# Keep comments in .env.example as documentation; write runtime .env as a
# simple, permission-protected key=value secret file.
keys = list(dict.fromkeys([*order, *defaults.keys(), "N8N_ENCRYPTION_KEY"]))
content = "".join(f"{key}={values.get(key, '')}\n" for key in keys)
mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
fd, temporary = tempfile.mkstemp(prefix=".env.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.chmod(temporary, mode & 0o600 or 0o600)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
print(effective_port)
PY
)"

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --pull always

for _ in $(seq 1 30); do
  if curl --fail --silent --show-error "http://127.0.0.1:${EFFECTIVE_PORT}/healthz" >/dev/null; then
    echo "n8n is healthy at http://127.0.0.1:${EFFECTIVE_PORT}"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" ps
    echo "Next: create the first n8n owner account, then run configure-hermes-service-auth.sh."
    exit 0
  fi
  sleep 2
done

echo "n8n did not become healthy within 60 seconds." >&2
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" logs --tail=100 n8n >&2 || true
exit 1
