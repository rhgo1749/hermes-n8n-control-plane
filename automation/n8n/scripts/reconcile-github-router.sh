#!/usr/bin/env bash
# Reconcile topic-managed GitHub repository webhooks without scheduling intake.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TOKEN_FILE="$ROOT/automation/n8n/state/secrets/hermes-intake-control-token"
ROUTER_URL="${GITHUB_ROUTER_URL:-http://127.0.0.1:5681}"

[[ -f "$TOKEN_FILE" ]] || {
  echo "Router service token missing: $TOKEN_FILE" >&2
  echo "Run configure-github-router-secrets.sh first." >&2
  exit 2
}

python3 - "$ROUTER_URL" "$TOKEN_FILE" <<'PY'
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

base = sys.argv[1].rstrip("/")
token_file = Path(sys.argv[2])
try:
    token = token_file.read_text(encoding="utf-8").strip()
except OSError as exc:
    raise SystemExit("router service token is unavailable") from exc
if not token:
    raise SystemExit("router service token is empty")

request = Request(
    f"{base}/reconcile",
    data=b"",
    method="POST",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    },
)
try:
    with urlopen(request, timeout=60) as response:
        payload = json.load(response)
except HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")[:1000]
    raise SystemExit(f"router reconcile returned HTTP {exc.code}: {body}") from exc
except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"router reconcile failed: {type(exc).__name__}") from exc
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
PY
