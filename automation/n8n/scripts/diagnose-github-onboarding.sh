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
    'Checks the existing intake job identity and loopback service health.' \
    'This command never creates, edits, pauses, or triggers a job.'
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

job_result="$(python3 - "$HERMES_HOME" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

home = Path(sys.argv[1]).expanduser()
expected_id = "bf431b2a6ba6"
stores = [("default", home / "cron" / "jobs.json")]
profiles = home / "profiles"
if profiles.is_dir():
    stores.extend(
        (path.parents[1].name, path)
        for path in sorted(profiles.glob("*/cron/jobs.json"))
    )

matches: list[str] = []
for profile, path in stores:
    if not path.is_file():
        continue
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        continue
    jobs = payload if isinstance(payload, list) else payload.get("jobs", [])
    if not isinstance(jobs, list):
        continue
    for job in jobs:
        if isinstance(job, dict) and job.get("id") == expected_id:
            matches.append(profile)

if matches != ["default"]:
    print(
        "authoritative_intake_job_missing "
        f"expected=default:{expected_id} matches={len(matches)}",
        file=sys.stderr,
    )
    raise SystemExit(1)
print(f"authoritative_intake_job=default:{expected_id} unique=true")
PY
)" || {
  printf '%s\n' "$job_result" >&2
  exit 1
}
printf '%s\n' "$job_result"

for intake_script in \
  "$HERMES_HOME/scripts/github-agent-ready-kanban-intake.py" \
  "$HERMES_HOME/scripts/github-agent-ready-kanban-intake-core.py"; do
  if [[ ! -f "$intake_script" ]]; then
    printf '%s\n' 'authoritative_intake_job_missing intake_script=false' >&2
    exit 1
  fi
done
printf '%s\n' 'intake_scripts_present=true'

if (( SKIP_NETWORK )); then
  printf '%s\n' 'network_probes=skipped'
  exit 0
fi

network_failed=0
for probe in \
  "router:$ROUTER_URL/healthz" \
  "lease-controller:$LEASE_URL/healthz" \
  "intake-actuator:$ACTUATOR_URL/healthz"; do
  service="${probe%%:*}"
  url="${probe#*:}"
  if [[ "$url" != http://127.0.0.1:* && "$url" != http://localhost:* && "$url" != http://\[::1\]:* ]] \
    || [[ "$url" == *\?* || "$url" == *\&* || "$url" == *\#* || "$url" == *@* ]]; then
    printf '%s_health=invalid_url\n' "$service" >&2
    network_failed=1
    continue
  fi
  if curl -q --fail --silent --show-error --max-time 3 "$url" >/dev/null; then
    printf '%s_health=ok\n' "$service"
  else
    printf '%s_health=unavailable\n' "$service" >&2
    network_failed=1
  fi
done

if (( network_failed )); then
  exit 1
fi
printf '%s\n' 'network_probes=ok'
