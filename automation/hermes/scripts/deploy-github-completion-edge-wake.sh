#!/usr/bin/env bash
# Install the completion-side GitHub edge-wake plugin into every Kanban
# specialist profile plus the root Hermes home.
#
# Why profiles: kanban_task_completed fires in the WORKER process, and the
# dispatcher spawns workers with a profile-scoped HERMES_HOME
# (hermes_cli.kanban_db injects resolve_profile_env(profile_arg)). Plugin
# discovery and the plugins.enabled allow-list are both resolved from that
# profile home, so an install only in the root home is invisible to every
# worker completion — the exact 2026-08-26 t_c58b444f lost-completion-wake
# incident. See docs/GITHUB_COMPLETION_LIFECYCLE.md.
#
# The root install remains in the list because the gateway/dashboard
# supervisor runs with the root HERMES_HOME and manual/CLI completions fire
# there. The per-profile edge runtime path check is skipped: the observer's
# runtime-root contract resolves <root>/scripts/kanban-github-sync.py through
# get_default_hermes_root() (the shared root), not the profile home, so only
# the ROOT home requires the deployed live edge script.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_BIN="${HERMES_BIN:-hermes}"
DRY_RUN=0
HOMES=()

usage() {
  cat <<'EOF'
Usage: deploy-github-completion-edge-wake.sh [--hermes-bin PATH] [--dry-run] [HOME ...]

Installs the github-completion-edge-wake plugin into the root Hermes home AND
every Kanban specialist profile (kanban-main, kanban-developer,
kanban-reviewer, kanban-designer) via install-github-completion-edge-wake.sh.

Worker processes run under a profile-scoped HERMES_HOME, so a root-only
install never sees worker completions. The root install covers the
gateway/dashboard supervisor namespace where CLI completions fire.

Options:
  --hermes-bin PATH   Hermes binary used for `plugins enable` (default: hermes)
  --dry-run           Show planned actions without changing anything
  HOME ...            Explicit homes instead of the default root+profile set

Each per-home install is atomic (candidate copy -> validate -> switch) with a
timestamped backup; a failure in one home does not roll back other homes.
Restart the Hermes worker/dashboard supervisor afterwards so already-running
processes load the hook. A signed live canary (next completed GitHub-backed
card projecting done -> review without manual intervention) remains a separate
HOST_VALIDATION_REQUIRED gate.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-bin)
      [[ $# -ge 2 ]] || { echo "--hermes-bin requires a path" >&2; exit 2; }
      HERMES_BIN="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    *) HOMES+=("$1"); shift ;;
  esac
done

if [[ ${#HOMES[@]} -eq 0 ]]; then
  ROOT_HOME="${HERMES_HOME:-$HOME/.hermes}"
  # Strip a trailing /profiles/<name> if the caller launched from a profile:
  # installs must land in the root home and every profile home explicitly.
  if [[ "$(basename "$(dirname "$ROOT_HOME")")" == "profiles" ]]; then
    ROOT_HOME="$(dirname "$(dirname "$ROOT_HOME")")"
  fi
  HOMES=("$ROOT_HOME")
  for profile_dir in "$ROOT_HOME"/profiles/*/; do
    [[ -d "$profile_dir" ]] || continue
    name="$(basename "$profile_dir")"
    case "$name" in
      kanban-main|kanban-developer|kanban-reviewer|kanban-designer)
        HOMES+=("${profile_dir%/}") ;;
      *)
        echo "skip: $name (not a Kanban specialist profile)" >&2 ;;
    esac
  done
fi

FAILED=()
for home in "${HOMES[@]}"; do
  echo "==> Installing into $home"
  if [[ ! -d "$home" ]]; then
    echo "FAIL: Hermes home not found: $home" >&2
    FAILED+=("$home")
    continue
  fi
  is_profile=0
  [[ "$(basename "$(dirname "$home")")" == "profiles" ]] && is_profile=1
  if [[ ! -d "$home/scripts" ]]; then
    # Profile homes legitimately lack scripts/ (edge lives in the shared
    # root); create it so the installer's target checks pass. Root homes must
    # already have it — deploy-intake-edge.sh owns that deployment.
    if [[ "$is_profile" -eq 1 ]]; then
      mkdir -p "$home/scripts"
    else
      # A root home without a deployed edge runtime cannot host a working
      # wake; skip it with an explicit failure rather than installing a
      # plugin that would fail closed on every completion.
      echo "FAIL: $home is missing scripts/ — run deploy-intake-edge.sh first" >&2
      FAILED+=("$home")
      continue
    fi
  fi
  args=(--hermes-home "$home" --hermes-bin "$HERMES_BIN")
  if [[ "$DRY_RUN" -eq 1 ]]; then args+=(--dry-run); fi
  set +e
  bash "$SCRIPT_DIR/install-github-completion-edge-wake.sh" "${args[@]}"
  rc=$?
  set -e
  if [[ $rc -eq 0 ]]; then
    echo "==> OK: $home"
  else
    echo "FAIL: installation failed for $home (exit $rc)" >&2
    FAILED+=("$home")
  fi
done

if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "Completed WITH FAILURES: ${FAILED[*]}" >&2
  exit 1
fi
echo "All homes installed. Restart the Hermes worker/dashboard supervisor, then run the live canary gate."
