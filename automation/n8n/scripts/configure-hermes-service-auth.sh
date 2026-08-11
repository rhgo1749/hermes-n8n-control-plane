#!/usr/bin/env bash
# Deploy the least-privilege service-auth plugin beside an existing Hermes home.
# Run this on the host as the account that owns the Hermes installation.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE="$ROOT/hermes-plugin/n8n-cron-auth"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-hermes}"
RESTART_COMMAND=""

usage() {
  cat <<'EOF'
Usage: configure-hermes-service-auth.sh [--hermes-home PATH] [--hermes-bin PATH]
                                        [--restart-command 'existing supervisor command']

Installs a user plugin plus a newly generated 256-bit token file. The token is
never printed. It authorizes only trigger/pause calls for the fixed GitHub
agent-ready intake job; it does not create a new API endpoint.

A dashboard restart is required for route registration. Pass --restart-command
only when you explicitly know the existing supervisor command; otherwise the
script prints the required manual action.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --hermes-bin) HERMES_BIN="$2"; shift 2 ;;
    --restart-command) RESTART_COMMAND="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -d "$SOURCE" && -f "$SOURCE/plugin.yaml" && -f "$SOURCE/__init__.py" ]] || {
  echo "plugin source missing: $SOURCE" >&2; exit 1;
}
[[ -d "$HERMES_HOME" ]] || { echo "Hermes home not found: $HERMES_HOME" >&2; exit 2; }
command -v "$HERMES_BIN" >/dev/null || { echo "Hermes binary not found: $HERMES_BIN" >&2; exit 2; }

# The existing Hermes token-auth seam authorizes by path and does not pass the
# query profile into DashboardAuthProvider.verify_token(). Require every fixed
# job ID to occur exactly once, in its intended profile, before installing the
# credential. A wrong profile query then cannot select a different current job.
verify_allowlisted_job_ids() {
  python3 - "$HERMES_HOME" <<'PY'
import json
import sys
from pathlib import Path

home = Path(sys.argv[1])
expected = {
    "bf431b2a6ba6": "default",
}
stores = [("default", home / "cron/jobs.json")]
stores.extend((path.parents[1].name, path) for path in sorted((home / "profiles").glob("*/cron/jobs.json")))
matches = {job_id: [] for job_id in expected}
for profile, path in stores:
    if not path.exists():
        continue
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        jobs = payload.get("jobs", [])
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot verify {path}: {exc}")
    if not isinstance(jobs, list):
        raise SystemExit(f"cannot verify {path}: jobs is not a list")
    for job in jobs:
        if isinstance(job, dict) and job.get("id") in matches:
            matches[job["id"]].append(profile)
failed = [
    f"{job_id}: found {profiles or ['missing']}, expected [{profile}]"
    for job_id, profile in expected.items()
    if matches[job_id] != [profile]
]
if failed:
    raise SystemExit("allowlisted job/profile verification failed: " + "; ".join(failed))
print(f"verified {len(expected)} allowlisted job IDs are unique to their expected profiles")
PY
}
verify_allowlisted_job_ids

TARGET="$HERMES_HOME/plugins/hermes-n8n-cron-auth"
install -d -m 700 "$TARGET"
install -m 644 "$SOURCE/plugin.yaml" "$TARGET/plugin.yaml"
install -m 644 "$SOURCE/__init__.py" "$TARGET/__init__.py"

TOKEN_FILE="$TARGET/.n8n-cron-token"
if [[ ! -f "$TOKEN_FILE" ]]; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

# User plugins are disabled by default. This changes only plugin enablement;
# --no-allow-tool-override explicitly refuses any built-in tool replacement.
HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins enable hermes-n8n-cron-auth --no-allow-tool-override

if [[ -n "$RESTART_COMMAND" ]]; then
  bash -lc "$RESTART_COMMAND"
else
  cat <<EOF
Plugin installed at: $TARGET
Token stored at:    $TOKEN_FILE (mode 0600; not printed)

Next, restart the EXISTING Hermes dashboard using its current supervisor so the
plugin registers its token routes. Then, in n8n, create one HTTP Header Auth
credential:
  header name:  Authorization
  header value: Bearer <contents of the protected token file>

Attach that credential to every imported HTTP Request node. Do not put the
token in workflow JSON, .env.example, shell history, or Git.
EOF
fi
