#!/usr/bin/env bash
# Pause the legacy Hermes schedules only after n8n has been proven to call the
# same existing Hermes cron path. `rollback` stops n8n before resuming Hermes.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
N8N_DIR="$ROOT/automation/n8n"
COMPOSE_FILE="$N8N_DIR/compose.yaml"
ENV_FILE="$N8N_DIR/.env"
STATE_DIR="$N8N_DIR/state/cutover"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-hermes}"
N8N_PORT=""
N8N_PORT_EXPLICIT=0
ACTION="cutover"
CONFIRMED=0
N8N_WORKFLOWS_DEACTIVATED=0

# Only jobs that were active in the audited inventory belong here. Existing
# paused DJ jobs intentionally remain outside the migration and rollback set.
TARGETS=(
  "default:168bd63461e7"
  "default:e432a90c1361"
  "default:df360bfa297d"
  "default:bf431b2a6ba6"
  "dj-broadcast:27f6725028ff"
)

usage() {
  cat <<'EOF'
Usage:
  cutover.sh --confirm-n8n-verified [--hermes-home PATH] [--hermes-bin PATH] [--n8n-port PORT]
  cutover.sh rollback --confirm-n8n-workflows-deactivated [--hermes-home PATH] [--hermes-bin PATH] [--n8n-port PORT]

Cutover requires an explicit confirmation that every Schedule Trigger workflow
has already been manually run at a non-scheduled time, the corresponding Hermes
job reached last_status=ok, the workflow paused it again, and the test job was
then resumed. This script does not activate n8n workflows for you: activate
Schedule Trigger workflows only after this command has paused legacy schedules.

Before rollback, deactivate every migration workflow in the n8n UI and verify
that state is persisted. Rollback then stops n8n (without removing data or
volumes) and restores the captured pre-cutover states.
EOF
}

if [[ "${1:-}" == "rollback" ]]; then
  ACTION="rollback"
  shift
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm-n8n-verified) CONFIRMED=1; shift ;;
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --hermes-bin) HERMES_BIN="$2"; shift 2 ;;
    --n8n-port) N8N_PORT="$2"; N8N_PORT_EXPLICIT=1; shift 2 ;;
    --confirm-n8n-workflows-deactivated) N8N_WORKFLOWS_DEACTIVATED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -d "$HERMES_HOME" ]] || { echo "Hermes home not found: $HERMES_HOME" >&2; exit 2; }
command -v "$HERMES_BIN" >/dev/null || { echo "Hermes binary not found: $HERMES_BIN" >&2; exit 2; }
[[ -f "$ENV_FILE" ]] || { echo "n8n .env not found; run host-install.sh first." >&2; exit 2; }

if [[ "$N8N_PORT_EXPLICIT" != 1 ]]; then
  N8N_PORT="$(python3 - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path

for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if raw.lstrip().startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    if key.strip() == "N8N_HOST_PORT":
        print(value.strip())
        break
else:
    print("5678")
PY
)"
fi
if ! [[ "$N8N_PORT" =~ ^[0-9]+$ ]] || ! (( N8N_PORT > 0 && N8N_PORT < 65536 )); then
  echo "effective n8n port must be 1..65535" >&2
  exit 2
fi

hermes_cron() {
  local profile="$1"; shift
  HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" -p "$profile" cron "$@"
}

verify_jobs() {
  local expected="$1"
  python3 - "$HERMES_HOME" "$expected" "${TARGETS[@]}" <<'PY'
import json
import sys
from pathlib import Path

home = Path(sys.argv[1])
expected = sys.argv[2]
targets = [tuple(item.split(':', 1)) for item in sys.argv[3:]]
failed = []
for profile, job_id in targets:
    path = home / ("cron/jobs.json" if profile == "default" else f"profiles/{profile}/cron/jobs.json")
    jobs = json.loads(path.read_text(encoding="utf-8")).get("jobs", [])
    job = next((row for row in jobs if row.get("id") == job_id), None)
    if job is None:
        failed.append(f"{profile}:{job_id}:missing")
        continue
    if expected == "active":
        valid = job.get("enabled") is True
    else:
        valid = job.get("enabled") is False and job.get("state") == "paused"
    if not valid:
        failed.append(f"{profile}:{job_id}:enabled={job.get('enabled')} state={job.get('state')}")
if failed:
    raise SystemExit("job state verification failed: " + "; ".join(failed))
print(f"verified {len(targets)} jobs: {expected}")
PY
}

snapshot() {
  local backup="$1"
  python3 - "$HERMES_HOME" "$backup" "${TARGETS[@]}" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

home, destination = Path(sys.argv[1]), Path(sys.argv[2])
targets = [tuple(item.split(':', 1)) for item in sys.argv[3:]]
rows = []
for profile, job_id in targets:
    path = home / ("cron/jobs.json" if profile == "default" else f"profiles/{profile}/cron/jobs.json")
    jobs = json.loads(path.read_text(encoding="utf-8")).get("jobs", [])
    job = next((row for row in jobs if row.get("id") == job_id), None)
    if job is None:
        raise SystemExit(f"cannot snapshot missing job: {profile}:{job_id}")
    rows.append({"profile": profile, "job": job})
payload = {"created_at": datetime.now(timezone.utc).isoformat(), "hermes_home": str(home), "jobs": rows}
destination.parent.mkdir(parents=True, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".cron-backup.", dir=destination.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

snapshot_rows() {
  local backup="$1"
  python3 - "$backup" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rows = payload.get("jobs")
if not isinstance(rows, list) or not rows:
    raise SystemExit("snapshot does not contain jobs")
for row in rows:
    if not isinstance(row, dict):
        raise SystemExit("snapshot contains an invalid row")
    profile, job = row.get("profile"), row.get("job")
    if not isinstance(profile, str) or not isinstance(job, dict) or not isinstance(job.get("id"), str):
        raise SystemExit("snapshot row is missing profile or job id")
    state = "active" if job.get("enabled") is True else "paused"
    print(profile, job["id"], state, sep="\t")
PY
}

verify_snapshot() {
  local backup="$1"
  python3 - "$HERMES_HOME" "$backup" <<'PY'
import json
import sys
from pathlib import Path

home, backup = Path(sys.argv[1]), Path(sys.argv[2])
payload = json.loads(backup.read_text(encoding="utf-8"))
rows = payload.get("jobs")
if not isinstance(rows, list) or not rows:
    raise SystemExit("snapshot does not contain jobs")
failed = []
for row in rows:
    profile, original = row.get("profile"), row.get("job")
    if not isinstance(profile, str) or not isinstance(original, dict):
        failed.append("invalid-snapshot-row")
        continue
    job_id = original.get("id")
    if not isinstance(job_id, str):
        failed.append(f"{profile}:missing-job-id")
        continue
    path = home / ("cron/jobs.json" if profile == "default" else f"profiles/{profile}/cron/jobs.json")
    try:
        jobs = json.loads(path.read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, json.JSONDecodeError):
        failed.append(f"{profile}:{job_id}:unreadable")
        continue
    current = next((item for item in jobs if item.get("id") == job_id), None)
    if not isinstance(current, dict):
        failed.append(f"{profile}:{job_id}:missing")
        continue
    if (current.get("enabled") is True) != (original.get("enabled") is True):
        failed.append(f"{profile}:{job_id}:enabled-mismatch")
if failed:
    raise SystemExit("snapshot state verification failed: " + "; ".join(failed))
print(f"verified {len(rows)} jobs against snapshot")
PY
}

restore_snapshot() {
  local backup="$1"
  local profile job_id expected action
  local failures=()
  snapshot_rows "$backup" >/dev/null
  while IFS=$'\t' read -r profile job_id expected; do
    if [[ "$expected" == "active" ]]; then
      action="resume"
    else
      action="pause"
    fi
    if ! hermes_cron "$profile" "$action" "$job_id"; then
      failures+=("$profile:$job_id:$action")
    fi
  done < <(snapshot_rows "$backup")
  if (( ${#failures[@]} )); then
    printf 'Snapshot restore command failures: %s\n' "${failures[*]}" >&2
    return 1
  fi
  verify_snapshot "$backup"
}

latest_backup() {
  python3 - "$STATE_DIR/latest.json" "$STATE_DIR" <<'PY'
import json
import sys
from pathlib import Path

latest, state_dir = Path(sys.argv[1]), Path(sys.argv[2]).resolve()
try:
    payload = json.loads(latest.read_text(encoding="utf-8"))
    raw_backup = payload["backup"]
except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
    raise SystemExit(f"cannot read cutover snapshot metadata: {exc}")
if not isinstance(raw_backup, str):
    raise SystemExit("cutover snapshot metadata has no backup path")
backup = Path(raw_backup).resolve()
try:
    backup.relative_to(state_dir)
except ValueError as exc:
    raise SystemExit("cutover backup path is outside the state directory") from exc
if not backup.is_file():
    raise SystemExit("cutover backup file is unavailable")
print(backup)
PY
}

if [[ "$ACTION" == "rollback" ]]; then
  [[ "$N8N_WORKFLOWS_DEACTIVATED" == 1 ]] || {
    echo "Refusing rollback until all migration workflows are persistently deactivated in n8n." >&2
    echo "After verifying that in the n8n UI, re-run with --confirm-n8n-workflows-deactivated." >&2
    exit 2
  }
  backup="$(latest_backup)"
  echo "Stopping n8n before restoring Hermes schedules (data/volumes are preserved)."
  docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" stop n8n
  if ! restore_snapshot "$backup"; then
    echo "Rollback could not restore the pre-cutover Hermes state; keep n8n stopped and recover from: $backup" >&2
    exit 1
  fi
  echo "Rollback complete: n8n stopped; captured legacy Hermes states restored."
  exit 0
fi

[[ "$CONFIRMED" == 1 ]] || {
  echo "Refusing cutover without --confirm-n8n-verified." >&2; usage >&2; exit 2;
}
verify_jobs active
curl --fail --silent --show-error "http://127.0.0.1:${N8N_PORT}/healthz" >/dev/null
mkdir -p "$STATE_DIR"
backup="$STATE_DIR/hermes-cron-before-cutover-$(date -u +%Y%m%dT%H%M%SZ).json"
snapshot "$backup"

for target in "${TARGETS[@]}"; do
  profile="${target%%:*}"
  job_id="${target#*:}"
  if ! hermes_cron "$profile" pause "$job_id"; then
    echo "Pause failed or had an unknown outcome; restoring the complete pre-cutover snapshot." >&2
    if ! restore_snapshot "$backup"; then
      echo "Automatic restore failed; keep n8n Schedule workflows inactive and recover from: $backup" >&2
    fi
    exit 1
  fi
done
if ! verify_jobs paused; then
  echo "Post-pause verification failed; restoring the complete pre-cutover snapshot." >&2
  if ! restore_snapshot "$backup"; then
    echo "Automatic restore failed; keep n8n Schedule workflows inactive and recover from: $backup" >&2
  fi
  exit 1
fi
printf '{"cutover_at":"%s","backup":"%s","targets":%s}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$backup" "$(printf '%s\n' "${TARGETS[@]}" | python3 -c 'import json,sys; print(json.dumps([x.strip() for x in sys.stdin if x.strip()]))')" \
  > "$STATE_DIR/latest.json"
chmod 600 "$STATE_DIR/latest.json"
echo "Legacy schedules are paused, preserved, and recoverable."
echo "Now activate the verified n8n Schedule Trigger workflows; do not activate them before this point."
echo "Before rollback, deactivate all migration workflows in n8n and verify their persisted inactive state."
echo "Rollback command: $0 rollback --confirm-n8n-workflows-deactivated --hermes-home '$HERMES_HOME' --n8n-port '$N8N_PORT'"
