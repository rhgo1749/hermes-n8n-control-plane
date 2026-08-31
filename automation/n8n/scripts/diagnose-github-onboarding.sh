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
expected_script = "github-agent-ready-kanban-intake.py"
max_metadata_bytes = 1 * 1024 * 1024
canonical_store = home / "cron" / "jobs.json"
# Job identity is bound to the canonical store location, the exact job id,
# the intake script, and the preserved schedule/runtime fields below.
# Deliberately NOT bound to: a persisted object-level `profile` key (Hermes
# cron/jobs.py never persists one; profile identity is the store path) or
# an exact short `name` (non-authoritative; the no-rename contract in
# docs/OPERATIONS.md means historical names must keep passing).  The `name`
# field is still read for the operator-readable summary only.


def has_symlink_component(path: Path) -> bool:
    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


if has_symlink_component(canonical_store):
    fail_reason = "store_symlink"
    print(
        f"authoritative_job_metadata_invalid reason={fail_reason}",
        file=sys.stderr,
    )
    raise SystemExit(1)

stores = [("default", canonical_store)]
profiles = home / "profiles"
if profiles.is_dir():
    stores.extend(
        (path.parents[1].name, path)
        for path in sorted(profiles.glob("*/cron/jobs.json"))
    )


def fail(code: str, **fields: object) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"{code}{(' ' + details) if details else ''}", file=sys.stderr)
    raise SystemExit(1)


def reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_jobs(profile: str, path: Path) -> list[object]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_metadata_bytes + 1)
        if len(raw) > max_metadata_bytes:
            fail("authoritative_job_metadata_invalid", profile=profile, reason="oversized")
        payload = json.loads(raw, object_pairs_hook=reject_duplicate_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        fail("authoritative_job_metadata_invalid", profile=profile, reason=type(exc).__name__)
    if isinstance(payload, list):
        jobs = payload
    elif isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        jobs = payload["jobs"]
    else:
        fail("authoritative_job_metadata_invalid", profile=profile, reason="jobs_not_list")
    if any(not isinstance(job, dict) for job in jobs):
        fail("authoritative_job_metadata_invalid", profile=profile, reason="job_not_object")
    return jobs


matches: list[tuple[str, Path, dict[str, object]]] = []
for profile, path in stores:
    if path.is_symlink():
        fail("authoritative_job_metadata_invalid", profile=profile, reason="store_symlink")
    if not path.is_file():
        continue
    for job in load_jobs(profile, path):
        if job.get("id") == expected_id:
            matches.append((profile, path, job))

if not matches:
    fail(
        "authoritative_intake_job_missing",
        expected=f"default:{expected_id}",
        matches=len(matches),
    )
if len(matches) != 1 or matches[0][0] != "default":
    fail(
        "authoritative_job_metadata_invalid",
        reason="duplicate_or_noncanonical",
        matches=len(matches),
    )

profile, path, job = matches[0]
if path != (home / "cron" / "jobs.json"):
    fail("authoritative_job_metadata_invalid", reason="noncanonical_store")
if job.get("script") != expected_script:
    fail("authoritative_job_metadata_invalid", reason="script_mismatch")
# NOTE: no object-level `profile` gate — profile identity is the canonical
# store path above (default profile home), not a persisted job field.
# NOTE: no exact `name` gate — name is non-authoritative (see header).
script_path = home / "scripts" / expected_script
if (
    has_symlink_component(script_path)
    or script_path.is_symlink()
    or not script_path.is_file()
):
    fail("authoritative_job_metadata_invalid", reason="script_missing")
if "workdir" not in job or job.get("workdir") is not None:
    fail("authoritative_job_metadata_invalid", reason="workdir_mismatch")
if job.get("no_agent") is not True:
    fail("authoritative_job_metadata_invalid", reason="no_agent_mismatch")
if "deliver" not in job or job.get("deliver") != "local":
    fail("authoritative_job_metadata_invalid", reason="deliver_mismatch")
if "origin" not in job or job.get("origin") is not None:
    fail("authoritative_job_metadata_invalid", reason="origin_mismatch")
if "base_url" not in job or job.get("base_url") is not None:
    fail("authoritative_job_metadata_invalid", reason="base_url_mismatch")

schedule = job.get("schedule")
if not isinstance(schedule, dict):
    fail("authoritative_job_metadata_invalid", reason="schedule_not_object")
schedule_kind = schedule.get("kind")
schedule_display = job.get("schedule_display", schedule.get("display"))
if schedule_kind == "interval":
    schedule_ok = (
        type(schedule.get("minutes")) is int
        and schedule.get("minutes") == 5
        and schedule_display == "every 5m"
    )
elif schedule_kind == "cron":
    schedule_ok = (
        type(schedule.get("expr")) is str
        and schedule.get("expr") == "*/5 * * * *"
        and schedule_display == "*/5 * * * *"
    )
else:
    schedule_ok = False
if not schedule_ok:
    fail("authoritative_job_metadata_invalid", reason="schedule_mismatch")

state = job.get("state")
enabled = job.get("enabled")
if not isinstance(state, str) or state not in {"paused", "scheduled"} or not isinstance(enabled, bool):
    fail("authoritative_job_metadata_invalid", reason="lifecycle_invalid")
if (state == "paused") != (enabled is False):
    fail("authoritative_job_metadata_invalid", reason="lifecycle_mismatch")
if state == "paused":
    lifecycle = "paused_between_wakes"
elif state == "scheduled" and enabled is True:
    lifecycle = "scheduled"
else:
    fail("authoritative_job_metadata_invalid", reason="lifecycle_invalid")
repeat = job.get("repeat")
if (
    not isinstance(repeat, dict)
    or "times" not in repeat
    or repeat.get("times") is not None
):
    fail("authoritative_job_metadata_invalid", reason="repeat_mismatch")

print(
    f"authoritative_intake_job=default:{expected_id} unique=true "
    f"profile={profile} script={expected_script} "
    f"schedule={schedule_display} workdir=null state={state} lifecycle={lifecycle} no_agent=true",
)
PY
)" || {
  printf '%s\n' "$job_result" >&2
  exit 1
}
printf '%s\n' "$job_result"

declare -A intake_script_contracts=(
  ["$HERMES_HOME/scripts/github-agent-ready-kanban-intake.py"]="_core_path,_load_core,_install_completion_contract_overlay,main"
  ["$HERMES_HOME/scripts/github-agent-ready-kanban-intake-core.py"]="_onboarding_repository_metadata,_ensure_checkout,_validate_onboarding_checkout,_materialize_onboarding_checkout,_provision_scoped_checkouts,_scope_transition,_task_body,main"
)
for intake_script in "${!intake_script_contracts[@]}"; do
  if [[ ! -f "$intake_script" || -L "$intake_script" ]]; then
    printf '%s\n' 'authoritative_intake_job_missing intake_script=false' >&2
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
    printf '%s\n' 'authoritative_intake_job_invalid intake_script=false' >&2
    exit 1
  fi
done
printf '%s\n' 'intake_scripts_present=true contract=verified'


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

trigger_url="$LEASE_URL/trigger?profile=default"
trigger_status=""
if [[ "$LEASE_URL" != http://127.0.0.1:* && "$LEASE_URL" != http://localhost:* && "$LEASE_URL" != http://\[::1\]:* ]] \
  || [[ "$LEASE_URL" == *\?* || "$LEASE_URL" == *\&* || "$LEASE_URL" == *\#* || "$LEASE_URL" == *@* ]]; then
  printf '%s\n' 'lease_trigger_contract=unavailable status=invalid_url' >&2
  network_failed=1
elif trigger_headers="$(curl -q --silent --show-error --max-time 3 \
  --output /dev/null --dump-header - --request OPTIONS "$trigger_url")"; then
  trigger_status="$(printf '%s' "$trigger_headers" | awk 'toupper($1) ~ /^HTTP\// { status=$2 } END { print status }')"
  allow_header="$(printf '%s' "$trigger_headers" | awk 'tolower($1) == "allow:" { sub(/^[^:]*:[[:space:]]*/, ""); print; exit }')"
  if [[ "$trigger_status" == "204" && ",${allow_header}," == *,POST,* ]]; then
    printf '%s\n' 'lease_trigger_contract=available method=POST profile=default'
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
