#!/usr/bin/env bash
# Deploy the intake + edge + repository registry scripts into the active Hermes
# runtime home. Run this in the namespace that owns that runtime path. In the
# current containerized deployment that means inside hermes-cloudcli-agent with
# --hermes-home /home/hermes/.hermes; a host-side $HOME/.hermes is not the live
# runtime unless it is explicitly the mounted backing path.
#
# Live deployment path (verified 2026-08-13): the Hermes cron job
# bf431b2a6ba6 ("GitHub agent-ready Issue intake") in the `default` profile
# stores `script: github-agent-ready-kanban-intake.py` with `workdir: null`,
# so the scheduler resolves and executes the file under
# $HERMES_HOME/scripts/github-agent-ready-kanban-intake.py — a deployed copy,
# NOT this repository checkout. The intake resolves its edge counterpart and
# repository registry from the same runtime directory, so all deployed files
# must be updated together.
#
# The canonical intake source remains
# automation/hermes/scripts/github-agent-ready-kanban-intake.py. Deployment
# installs it as github-agent-ready-kanban-intake-core.py and installs the small
# completion-contract entrypoint under the historical live name
# github-agent-ready-kanban-intake.py. The wrapper keeps worker completion on
# core kanban_complete while the edge remains the sole GitHub done<->review
# projection owner, preventing core review-worker self-reclaim loops.
#
# The canonical edge reconciliation source remains edge/kanban-github-sync.py.
# Deployment installs it as kanban-github-sync-core.py and installs the small
# resource-admission entrypoint under the historical live name
# kanban-github-sync.py. The entrypoint installs the resource-admission,
# head-binding-feedback, and retry-signal-guard overlays onto that canonical
# core. With no configured worker_resources, resource scheduling behavior is
# unchanged.
#
# Safety guarantees:
#   * candidate copy + validation (py_compile, --help smoke) before any write
#   * atomic replace via same-filesystem mv
#   * wrapper dependencies are installed before either live wrapper switch
#   * timestamped backup of the previous files (existing .bak-* convention)
#   * rollback = restore the backup (exact command printed)
#   * NEVER touches cron jobs.json / job id / schedule / enabled state
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
INT_ENTRY_SOURCE="$ROOT/automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py"
INT_CORE_SOURCE="$ROOT/automation/hermes/scripts/github-agent-ready-kanban-intake.py"
EDGE_ENTRY_SOURCE="$ROOT/edge/kanban-github-sync-entrypoint.py"
EDGE_CORE_SOURCE="$ROOT/edge/kanban-github-sync.py"
EDGE_ADMISSION_SOURCE="$ROOT/edge/kanban_resource_admission.py"
EDGE_HEAD_BINDING_SOURCE="$ROOT/edge/kanban_head_binding_feedback.py"
EDGE_RETRY_GUARD_SOURCE="$ROOT/edge/kanban_retry_signal_guard.py"
REGISTRY_SOURCE="$ROOT/automation/n8n/scripts/repository_registry.py"
MIGRATION_SOURCE="$ROOT/automation/n8n/scripts/board_identity_migration.py"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: deploy-intake-edge.sh [--hermes-home PATH] [--dry-run]

Deploys the tracked intake + edge + repository registry scripts into
$HERMES_HOME/scripts/ using candidate copy -> validation -> atomic replace,
keeping timestamped backups (.bak-<name>-<ts>). Rollback command is printed
after deploy.

The live github-agent-ready-kanban-intake.py is a small completion-contract
entrypoint. The canonical intake implementation is deployed beside it as
github-agent-ready-kanban-intake-core.py. GitHub-backed workers use core
kanban_complete as their terminal action; the edge owns parked review/done
projection from fresh GitHub state.

The live kanban-github-sync.py is a small overlay entrypoint. The canonical
reconciliation implementation is deployed beside it as kanban-github-sync-core.py,
plus kanban_resource_admission.py, kanban_head_binding_feedback.py, and
kanban_retry_signal_guard.py. If no kanban.worker_resources are configured,
scheduling behavior is unchanged. All wrapper dependencies are replaced before
the corresponding live entrypoint, so a cron invocation during deploy sees either
the old standalone script or a fully backed new wrapper — never a wrapper whose
imports have not been installed.

Run this where the supplied --hermes-home path is the active Hermes runtime.
For the current containerized deployment:
  docker exec hermes-cloudcli-agent bash /ws/projects/<checkout>/automation/hermes/scripts/deploy-intake-edge.sh --hermes-home /home/hermes/.hermes

The Hermes cron job definition (id/schedule/enabled) is never modified.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for source in \
  "$INT_ENTRY_SOURCE" \
  "$INT_CORE_SOURCE" \
  "$EDGE_ENTRY_SOURCE" \
  "$EDGE_CORE_SOURCE" \
  "$EDGE_ADMISSION_SOURCE" \
  "$EDGE_HEAD_BINDING_SOURCE" \
  "$EDGE_RETRY_GUARD_SOURCE" \
  "$REGISTRY_SOURCE" \
  "$MIGRATION_SOURCE"
do
  [[ -f "$source" ]] || {
    echo "intake/edge/registry source missing in checkout: $source" >&2
    exit 1
  }
done

TARGET_DIR="$HERMES_HOME/scripts"
[[ -d "$TARGET_DIR" ]] || { echo "Hermes scripts dir not found: $TARGET_DIR" >&2; exit 2; }

# 1) candidate copy into a temp dir on the same filesystem
TS="$(date -u +%Y%m%dT%H%M%SZ)"
CANDIDATE="$TARGET_DIR/.deploy-candidate-${TS}"
install -d -m 700 "$CANDIDATE"
cp -p "$INT_ENTRY_SOURCE" "$CANDIDATE/github-agent-ready-kanban-intake.py"
cp -p "$INT_CORE_SOURCE" "$CANDIDATE/github-agent-ready-kanban-intake-core.py"
cp -p "$EDGE_ENTRY_SOURCE" "$CANDIDATE/kanban-github-sync.py"
cp -p "$EDGE_CORE_SOURCE" "$CANDIDATE/kanban-github-sync-core.py"
cp -p "$EDGE_ADMISSION_SOURCE" "$CANDIDATE/kanban_resource_admission.py"
cp -p "$EDGE_HEAD_BINDING_SOURCE" "$CANDIDATE/kanban_head_binding_feedback.py"
cp -p "$EDGE_RETRY_GUARD_SOURCE" "$CANDIDATE/kanban_retry_signal_guard.py"
cp -p "$REGISTRY_SOURCE" "$CANDIDATE/repository_registry.py"
cp -p "$MIGRATION_SOURCE" "$CANDIDATE/board_identity_migration.py"

# 2) validation: compile + argparse smoke (--help exits 0)
python3 -m py_compile \
  "$CANDIDATE/github-agent-ready-kanban-intake.py" \
  "$CANDIDATE/github-agent-ready-kanban-intake-core.py" \
  "$CANDIDATE/kanban-github-sync.py" \
  "$CANDIDATE/kanban-github-sync-core.py" \
  "$CANDIDATE/kanban_resource_admission.py" \
  "$CANDIDATE/kanban_head_binding_feedback.py" \
  "$CANDIDATE/kanban_retry_signal_guard.py" \
  "$CANDIDATE/repository_registry.py" \
  "$CANDIDATE/board_identity_migration.py" || {
  rm -rf "$CANDIDATE"; echo "candidate validation failed (py_compile)" >&2; exit 1;
}
python3 "$CANDIDATE/github-agent-ready-kanban-intake.py" --help >/dev/null 2>&1 || {
  rm -rf "$CANDIDATE"; echo "candidate validation failed (intake wrapper --help)" >&2; exit 1;
}
python3 "$CANDIDATE/github-agent-ready-kanban-intake-core.py" --help >/dev/null 2>&1 || {
  rm -rf "$CANDIDATE"; echo "candidate validation failed (intake core --help)" >&2; exit 1;
}
python3 "$CANDIDATE/kanban-github-sync.py" --help >/dev/null 2>&1 || {
  rm -rf "$CANDIDATE"; echo "candidate validation failed (edge wrapper --help)" >&2; exit 1;
}
python3 "$CANDIDATE/kanban-github-sync-core.py" --help >/dev/null 2>&1 || {
  rm -rf "$CANDIDATE"; echo "candidate validation failed (edge core --help)" >&2; exit 1;
}
python3 "$CANDIDATE/repository_registry.py" --help >/dev/null 2>&1 || {
  rm -rf "$CANDIDATE"; echo "candidate validation failed (registry --help)" >&2; exit 1;
}
python3 "$CANDIDATE/board_identity_migration.py" --help >/dev/null 2>&1 || {
  rm -rf "$CANDIDATE"; echo "candidate validation failed (migration --help)" >&2; exit 1;
}

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: candidate validated at $CANDIDATE"
  echo "dry-run: would atomically replace (dependencies before wrappers):"
  echo "dry-run:   $TARGET_DIR/kanban-github-sync-core.py"
  echo "dry-run:   $TARGET_DIR/kanban_resource_admission.py"
  echo "dry-run:   $TARGET_DIR/kanban_head_binding_feedback.py"
  echo "dry-run:   $TARGET_DIR/kanban_retry_signal_guard.py"
  echo "dry-run:   $TARGET_DIR/repository_registry.py"
  echo "dry-run:   $TARGET_DIR/board_identity_migration.py"
  echo "dry-run:   $TARGET_DIR/github-agent-ready-kanban-intake-core.py"
  echo "dry-run:   $TARGET_DIR/github-agent-ready-kanban-intake.py"
  echo "dry-run:   $TARGET_DIR/kanban-github-sync.py"
  rm -rf "$CANDIDATE"
  exit 0
fi

# 3) backups + atomic replace (mv is atomic on the same filesystem).
# Install dependencies first and switch each historical live wrapper only after
# its backing files are present. The edge wrapper remains LAST because it
# imports the edge overlays in addition to its canonical core.
BACKUPS=()
for name in \
  kanban-github-sync-core.py \
  kanban_resource_admission.py \
  kanban_head_binding_feedback.py \
  kanban_retry_signal_guard.py \
  repository_registry.py \
  board_identity_migration.py \
  github-agent-ready-kanban-intake-core.py \
  github-agent-ready-kanban-intake.py \
  kanban-github-sync.py
do
  if [[ -f "$TARGET_DIR/$name" ]]; then
    backup="$TARGET_DIR/.bak-$name-$TS"
    cp -p "$TARGET_DIR/$name" "$backup"
    BACKUPS+=("$backup")
  fi
  mv -f "$CANDIDATE/$name" "$TARGET_DIR/$name"
done
rm -rf "$CANDIDATE"

source_path_for() {
  case "$1" in
    github-agent-ready-kanban-intake.py)
      printf '%s\n' "$ROOT/automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py"
      ;;
    github-agent-ready-kanban-intake-core.py)
      printf '%s\n' "$ROOT/automation/hermes/scripts/github-agent-ready-kanban-intake.py"
      ;;
    kanban-github-sync.py)
      printf '%s\n' "$ROOT/edge/kanban-github-sync-entrypoint.py"
      ;;
    kanban-github-sync-core.py)
      printf '%s\n' "$ROOT/edge/kanban-github-sync.py"
      ;;
    kanban_resource_admission.py)
      printf '%s\n' "$ROOT/edge/kanban_resource_admission.py"
      ;;
    kanban_head_binding_feedback.py)
      printf '%s\n' "$ROOT/edge/kanban_head_binding_feedback.py"
      ;;
    kanban_retry_signal_guard.py)
      printf '%s\n' "$ROOT/edge/kanban_retry_signal_guard.py"
      ;;
    repository_registry.py)
      printf '%s\n' "$ROOT/automation/n8n/scripts/repository_registry.py"
      ;;
    board_identity_migration.py)
      printf '%s\n' "$ROOT/automation/n8n/scripts/board_identity_migration.py"
      ;;
    *)
      return 2
      ;;
  esac
}

# 4) verify installed bytes match the checkout
for name in \
  github-agent-ready-kanban-intake.py \
  github-agent-ready-kanban-intake-core.py \
  kanban-github-sync.py \
  kanban-github-sync-core.py \
  kanban_resource_admission.py \
  kanban_head_binding_feedback.py \
  kanban_retry_signal_guard.py \
  repository_registry.py \
  board_identity_migration.py
do
  source_path="$(source_path_for "$name")"
  source_hash="$(sha256sum "$source_path" | cut -d' ' -f1)"
  target_hash="$(sha256sum "$TARGET_DIR/$name" | cut -d' ' -f1)"
  [[ "$source_hash" == "$target_hash" ]] || {
    echo "post-install hash mismatch for $name" >&2; exit 1;
  }
done

echo "Deployed intake/edge/registry to $TARGET_DIR (backup: ${BACKUPS[*]:-none})"
echo "Cron job bf431b2a6ba6 is untouched (id/schedule/enabled unchanged)."
if [[ ${#BACKUPS[@]} -gt 0 ]]; then
  echo "Rollback:"
  for backup in "${BACKUPS[@]}"; do
    base="$(basename "$backup")"            # .bak-<name>-<ts>
    name="${base#.bak-}"; name="${name%-*}" # strip .bak- prefix and -<ts>
    echo "  mv \"$backup\" \"$TARGET_DIR/$name\""
  done
fi
