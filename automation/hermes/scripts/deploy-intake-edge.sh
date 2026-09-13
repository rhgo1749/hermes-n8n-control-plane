#!/usr/bin/env bash
# Deploy the intake + edge + repository registry scripts into the active Hermes
# runtime home. Run this in the namespace that owns that runtime path. In the
# current containerized deployment that means inside hermes-cloudcli-agent with
# --hermes-home /home/hermes/.hermes; a host-side $HOME/.hermes is not the live
# runtime unless it is explicitly the mounted backing path.
#
# Live deployment path: the fixed loopback direct actuator executes
# $HERMES_HOME/scripts/github-agent-ready-kanban-intake.py — a deployed copy,
# NOT this repository checkout. The intake resolves its edge counterpart and
# repository registry from the same runtime directory, so all deployed files
# must be updated together. The retired legacy intake cron job is not required.
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
#   * NEVER touches cron jobs.json; any historical legacy intake job is left as-is
#   * lifecycle guard dependencies are installed before the approved wrapper
#   * workspace-binding preflight is installed before the approved wrapper
#   * the existing approved shell-hook command path stays unchanged
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
INT_ENTRY_SOURCE="$ROOT/automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py"
INT_CORE_SOURCE="$ROOT/automation/hermes/scripts/github-agent-ready-kanban-intake.py"
EDGE_ENTRY_SOURCE="$ROOT/edge/kanban-github-sync-entrypoint.py"
EDGE_CORE_SOURCE="$ROOT/edge/kanban-github-sync.py"
EDGE_ADMISSION_SOURCE="$ROOT/edge/kanban_resource_admission.py"
EDGE_HEAD_BINDING_SOURCE="$ROOT/edge/kanban_head_binding_feedback.py"
EDGE_RETRY_GUARD_SOURCE="$ROOT/edge/kanban_retry_signal_guard.py"
EDGE_WS_ADMISSION_SOURCE="$ROOT/edge/kanban_workspace_admission.py"
EDGE_DYNAMIC_CORE_SOURCE="$ROOT/edge/kanban_dynamic_resource_core.py"
EDGE_DYNAMIC_SOURCE="$ROOT/edge/kanban_dynamic_resource.py"
BLOCK_KIND_GUARD_SOURCE="$ROOT/automation/hermes/scripts/kanban-block-kind-guard.py"
BLOCK_KIND_GUARD_CORE_SOURCE="$ROOT/automation/hermes/scripts/kanban-block-kind-guard-core.py"
SPECIALIST_COMPLETION_GUARD_SOURCE="$ROOT/automation/hermes/scripts/kanban-specialist-completion-guard.py"
WORKSPACE_BINDING_GUARD_SOURCE="$ROOT/automation/hermes/scripts/kanban-workspace-binding-guard.py"
BLOCK_KIND_CONFIG_SOURCE="$ROOT/automation/hermes/scripts/kanban-block-kind-hook-config.py"
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
plus kanban_resource_admission.py, kanban_head_binding_feedback.py,
kanban_retry_signal_guard.py, and the dynamic resource core/wrapper pair. If no
kanban.worker_resources are configured, scheduling behavior is unchanged. All
wrapper dependencies are replaced before the corresponding live entrypoint, so
a concurrent actuator invocation during deploy sees either the old standalone
script or a fully backed new wrapper — never a wrapper whose imports have not
been installed.

Run this where the supplied --hermes-home path is the active Hermes runtime.
For the current containerized deployment:
  docker exec hermes-cloudcli-agent bash /ws/projects/<checkout>/automation/hermes/scripts/deploy-intake-edge.sh --hermes-home /home/hermes/.hermes

The deployer never reads or modifies Hermes cron metadata. The current
intake topology is direct-actuator based (`hermes_cron_required=false`).

The deployment keeps the already-approved
$HERMES_HOME/scripts/kanban-block-kind-guard.py shell-hook command stable. That
wrapper now covers three fail-closed pre_tool_call matchers:
  * kanban_block -> explicit block-kind policy
  * kanban_create -> H4V3 specialist completion and workspace-binding policies
  * terminal -> both policies
Its block-kind and specialist policy implementations are deployed beside the
wrapper before it is switched. This avoids a new shell-hook consent boundary.
Use --dry-run for candidate validation only; applying the live config hook is a
human validation gate.
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
  "$EDGE_WS_ADMISSION_SOURCE" \
  "$EDGE_DYNAMIC_CORE_SOURCE" \
  "$EDGE_DYNAMIC_SOURCE" \
  "$BLOCK_KIND_GUARD_SOURCE" \
  "$BLOCK_KIND_GUARD_CORE_SOURCE" \
  "$SPECIALIST_COMPLETION_GUARD_SOURCE" \
  "$WORKSPACE_BINDING_GUARD_SOURCE" \
  "$BLOCK_KIND_CONFIG_SOURCE" \
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
CONFIG_TARGET="$HERMES_HOME/config.yaml"
[[ -f "$CONFIG_TARGET" ]] || { echo "Hermes config not found: $CONFIG_TARGET" >&2; exit 2; }
H4V3_PROFILE_NAMES=(
  kanban-main
  kanban-investigator
  kanban-developer
  kanban-reviewer
  kanban-designer
)
PROFILE_CONFIG_TARGETS=()
for profile in "${H4V3_PROFILE_NAMES[@]}"; do
  profile_config="$HERMES_HOME/profiles/$profile/config.yaml"
  [[ -f "$profile_config" ]] || {
    echo "Hermes profile config not found: $profile_config" >&2
    exit 2
  }
  PROFILE_CONFIG_TARGETS+=("$profile_config")
done

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
cp -p "$EDGE_WS_ADMISSION_SOURCE" "$CANDIDATE/kanban_workspace_admission.py"
# The implementation must land before the stable wrapper that imports it.
cp -p "$EDGE_DYNAMIC_CORE_SOURCE" "$CANDIDATE/kanban_dynamic_resource_core.py"
cp -p "$EDGE_DYNAMIC_SOURCE" "$CANDIDATE/kanban_dynamic_resource.py"
cp -p "$BLOCK_KIND_GUARD_CORE_SOURCE" "$CANDIDATE/kanban-block-kind-guard-core.py"
cp -p "$SPECIALIST_COMPLETION_GUARD_SOURCE" "$CANDIDATE/kanban-specialist-completion-guard.py"
cp -p "$WORKSPACE_BINDING_GUARD_SOURCE" "$CANDIDATE/kanban-workspace-binding-guard.py"
cp -p "$BLOCK_KIND_GUARD_SOURCE" "$CANDIDATE/kanban-block-kind-guard.py"
cp -p "$REGISTRY_SOURCE" "$CANDIDATE/repository_registry.py"
cp -p "$MIGRATION_SOURCE" "$CANDIDATE/board_identity_migration.py"
render_lifecycle_config() {
  local source_config="$1"
  local candidate_config="$2"
  local label="$3"
  python3 "$BLOCK_KIND_CONFIG_SOURCE" "$source_config" "$candidate_config" \
    --guard "$TARGET_DIR/kanban-block-kind-guard.py"
  python3 - "$candidate_config" "$label" <<'PY'
import sys
from pathlib import Path

import yaml

config_path = Path(sys.argv[1])
label = sys.argv[2]
config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
entries = config.get("hooks", {}).get("pre_tool_call", [])
guard_entries = [
    entry for entry in entries
    if isinstance(entry, dict)
    and entry.get("matcher") in {"kanban_block", "kanban_create", "terminal"}
    and "kanban-block-kind-guard.py" in str(entry.get("command", ""))
]
if {
    entry.get("matcher") for entry in guard_entries
} != {"kanban_block", "kanban_create", "terminal"} or any(
    entry.get("fail_closed") is not True for entry in guard_entries
):
    raise SystemExit(
        f"candidate config {label} is missing fail-closed H4V3 lifecycle guard entries"
    )
commands = {str(entry.get("command", "")) for entry in guard_entries}
if len(commands) != 1:
    raise SystemExit(
        f"candidate config {label} must reuse one approved lifecycle guard command"
    )
if any(
    "kanban-workspace-guard.py" in str(entry.get("command", ""))
    for entry in entries
    if isinstance(entry, dict)
):
    raise SystemExit(
        f"candidate config {label} still references superseded kanban-workspace-guard.py"
    )
PY
}

render_lifecycle_config "$CONFIG_TARGET" "$CANDIDATE/config.yaml" "global"
for profile in "${H4V3_PROFILE_NAMES[@]}"; do
  render_lifecycle_config \
    "$HERMES_HOME/profiles/$profile/config.yaml" \
    "$CANDIDATE/profile-$profile-config.yaml" \
    "profile:$profile"
done

# 2) validation: compile + argparse smoke (--help exits 0)
python3 -m py_compile \
  "$CANDIDATE/github-agent-ready-kanban-intake.py" \
  "$CANDIDATE/github-agent-ready-kanban-intake-core.py" \
  "$CANDIDATE/kanban-github-sync.py" \
  "$CANDIDATE/kanban-github-sync-core.py" \
  "$CANDIDATE/kanban_resource_admission.py" \
  "$CANDIDATE/kanban_head_binding_feedback.py" \
  "$CANDIDATE/kanban_retry_signal_guard.py" \
  "$CANDIDATE/kanban_workspace_admission.py" \
  "$CANDIDATE/kanban_dynamic_resource_core.py" \
  "$CANDIDATE/kanban_dynamic_resource.py" \
  "$CANDIDATE/kanban-block-kind-guard-core.py" \
  "$CANDIDATE/kanban-specialist-completion-guard.py" \
  "$CANDIDATE/kanban-workspace-binding-guard.py" \
  "$CANDIDATE/kanban-block-kind-guard.py" \
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
  echo "dry-run:   $TARGET_DIR/kanban_workspace_admission.py"
  echo "dry-run:   $TARGET_DIR/kanban_dynamic_resource_core.py"
  echo "dry-run:   $TARGET_DIR/kanban_dynamic_resource.py"
  echo "dry-run:   $TARGET_DIR/kanban-block-kind-guard-core.py"
  echo "dry-run:   $TARGET_DIR/kanban-specialist-completion-guard.py"
  echo "dry-run:   $TARGET_DIR/kanban-workspace-binding-guard.py"
  echo "dry-run:   $TARGET_DIR/kanban-block-kind-guard.py"
  echo "dry-run:   $TARGET_DIR/repository_registry.py"
  echo "dry-run:   $TARGET_DIR/board_identity_migration.py"
  echo "dry-run:   $TARGET_DIR/github-agent-ready-kanban-intake-core.py"
  echo "dry-run:   $TARGET_DIR/github-agent-ready-kanban-intake.py"
  echo "dry-run:   $TARGET_DIR/kanban-github-sync.py"
  echo "dry-run: would atomically replace $CONFIG_TARGET with lifecycle hook entries"
  for profile in "${H4V3_PROFILE_NAMES[@]}"; do
    echo "dry-run: would atomically replace $HERMES_HOME/profiles/$profile/config.yaml with lifecycle hook entries"
  done
  echo "dry-run:   lifecycle-guard matcher=kanban_block (fail_closed=true)"
  echo "dry-run:   lifecycle-guard matcher=kanban_create (fail_closed=true)"
  echo "dry-run:   lifecycle-guard matcher=terminal (fail_closed=true)"
  echo "dry-run: superseded kanban-workspace-guard.py hooks would be retired from global and H4V3 profile configs"
  echo "dry-run: shell-hook command path unchanged; no second consent command added"
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
  kanban_workspace_admission.py \
  kanban_dynamic_resource_core.py \
  kanban_dynamic_resource.py \
  kanban-block-kind-guard-core.py \
  kanban-specialist-completion-guard.py \
  kanban-workspace-binding-guard.py \
  kanban-block-kind-guard.py \
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
CONFIG_BACKUP="$HERMES_HOME/.bak-config.yaml-$TS"
cp -p "$CONFIG_TARGET" "$CONFIG_BACKUP"
PROFILE_CONFIG_BACKUPS=()
for index in "${!H4V3_PROFILE_NAMES[@]}"; do
  profile_target="${PROFILE_CONFIG_TARGETS[$index]}"
  profile_backup="${profile_target%/config.yaml}/.bak-config.yaml-$TS"
  cp -p "$profile_target" "$profile_backup"
  PROFILE_CONFIG_BACKUPS+=("$profile_backup")
done
mv -f "$CANDIDATE/config.yaml" "$CONFIG_TARGET"
for index in "${!H4V3_PROFILE_NAMES[@]}"; do
  profile="${H4V3_PROFILE_NAMES[$index]}"
  profile_target="${PROFILE_CONFIG_TARGETS[$index]}"
  mv -f "$CANDIDATE/profile-$profile-config.yaml" "$profile_target"
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
    kanban_workspace_admission.py)
      printf '%s\n' "$ROOT/edge/kanban_workspace_admission.py"
      ;;
    kanban_dynamic_resource_core.py)
      printf '%s\n' "$ROOT/edge/kanban_dynamic_resource_core.py"
      ;;
    kanban_dynamic_resource.py)
      printf '%s\n' "$ROOT/edge/kanban_dynamic_resource.py"
      ;;
    kanban-block-kind-guard-core.py)
      printf '%s\n' "$ROOT/automation/hermes/scripts/kanban-block-kind-guard-core.py"
      ;;
    kanban-specialist-completion-guard.py)
      printf '%s\n' "$ROOT/automation/hermes/scripts/kanban-specialist-completion-guard.py"
      ;;
    kanban-workspace-binding-guard.py)
      printf '%s\n' "$ROOT/automation/hermes/scripts/kanban-workspace-binding-guard.py"
      ;;
    kanban-block-kind-guard.py)
      printf '%s\n' "$ROOT/automation/hermes/scripts/kanban-block-kind-guard.py"
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
  kanban_workspace_admission.py \
  kanban_dynamic_resource_core.py \
  kanban_dynamic_resource.py \
  kanban-block-kind-guard-core.py \
  kanban-specialist-completion-guard.py \
  kanban-workspace-binding-guard.py \
  kanban-block-kind-guard.py \
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

echo "Deployed intake/edge/registry and lifecycle guards to $TARGET_DIR (backup: ${BACKUPS[*]:-none})"
echo "Config hooks installed at $CONFIG_TARGET (backup: $CONFIG_BACKUP)"
echo "Profile config hooks installed for: ${H4V3_PROFILE_NAMES[*]}"
echo "Shell-hook command remains $TARGET_DIR/kanban-block-kind-guard.py (existing consent identity preserved)."
echo "Superseded kanban-workspace-guard.py hook entries are retired from global/profile live config; the old file is left untouched for rollback archaeology."
echo "Hermes cron metadata is untouched; the current intake path does not require the retired legacy intake job."
if [[ ${#BACKUPS[@]} -gt 0 ]]; then
  echo "Rollback:"
  for backup in "${BACKUPS[@]}"; do
    base="$(basename "$backup")"            # .bak-<name>-<ts>
    name="${base#.bak-}"; name="${name%-*}" # strip .bak- prefix and -<ts>
    echo "  mv \"$backup\" \"$TARGET_DIR/$name\""
  done
fi
echo "  mv \"$CONFIG_BACKUP\" \"$CONFIG_TARGET\""
for index in "${!PROFILE_CONFIG_BACKUPS[@]}"; do
  echo "  mv \"${PROFILE_CONFIG_BACKUPS[$index]}\" \"${PROFILE_CONFIG_TARGETS[$index]}\""
done
