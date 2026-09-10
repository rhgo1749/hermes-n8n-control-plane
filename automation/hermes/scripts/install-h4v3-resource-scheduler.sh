#!/usr/bin/env bash
# Install the H4V3 backend-aware Kanban resource scheduler into the live Hermes
# default profile. This is external to Hermes core: helper modules are copied
# under $HERMES_HOME/scripts and a tiny user plugin patches the core claim
# boundary when the dispatcher process loads enabled plugins.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ADMISSION_SOURCE="$ROOT/edge/kanban_resource_admission.py"
DYNAMIC_CORE_SOURCE="$ROOT/edge/kanban_dynamic_resource_core.py"
DYNAMIC_SOURCE="$ROOT/edge/kanban_dynamic_resource.py"
PLUGIN_SOURCE="$ROOT/hermes-plugin/h4v3-resource-scheduler"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-hermes}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install-h4v3-resource-scheduler.sh [--hermes-home PATH] [--hermes-bin PATH] [--dry-run]

Installs:
  $HERMES_HOME/scripts/kanban_resource_admission.py
  $HERMES_HOME/scripts/kanban_dynamic_resource_core.py
  $HERMES_HOME/scripts/kanban_dynamic_resource.py
  $HERMES_HOME/plugins/h4v3-resource-scheduler/

Then enables h4v3-resource-scheduler without tool-override permission.

The installer does NOT edit config.yaml and does NOT restart Hermes. After
installing, add `backend: local` to the protected worker resource, raise the
coarse Hermes host/profile spawn caps to the desired cloud parallelism, and
restart the dispatcher-owning gateway so its core claim functions are patched.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home) HERMES_HOME="$2"; shift 2 ;;
    --hermes-bin) HERMES_BIN="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -f "$ADMISSION_SOURCE" ]] || { echo "missing: $ADMISSION_SOURCE" >&2; exit 1; }
[[ -f "$DYNAMIC_CORE_SOURCE" ]] || { echo "missing: $DYNAMIC_CORE_SOURCE" >&2; exit 1; }
[[ -f "$DYNAMIC_SOURCE" ]] || { echo "missing: $DYNAMIC_SOURCE" >&2; exit 1; }
[[ -f "$PLUGIN_SOURCE/plugin.yaml" && -f "$PLUGIN_SOURCE/__init__.py" ]] || {
  echo "resource scheduler plugin source missing: $PLUGIN_SOURCE" >&2
  exit 1
}
[[ -d "$HERMES_HOME" ]] || { echo "Hermes home not found: $HERMES_HOME" >&2; exit 2; }
[[ -d "$HERMES_HOME/scripts" ]] || { echo "Hermes scripts dir not found: $HERMES_HOME/scripts" >&2; exit 2; }

python3 -m py_compile \
  "$ADMISSION_SOURCE" \
  "$DYNAMIC_CORE_SOURCE" \
  "$DYNAMIC_SOURCE" \
  "$PLUGIN_SOURCE/__init__.py" || {
    echo "candidate validation failed (py_compile)" >&2
    exit 1
  }

TARGET_PLUGIN="$HERMES_HOME/plugins/h4v3-resource-scheduler"
PLUGIN_BACKUP_ROOT="$HERMES_HOME/plugin-backups/h4v3-resource-scheduler"
TARGET_ADMISSION="$HERMES_HOME/scripts/kanban_resource_admission.py"
TARGET_DYNAMIC_CORE="$HERMES_HOME/scripts/kanban_dynamic_resource_core.py"
TARGET_DYNAMIC="$HERMES_HOME/scripts/kanban_dynamic_resource.py"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: would install $ADMISSION_SOURCE -> $TARGET_ADMISSION"
  echo "dry-run: would install $DYNAMIC_CORE_SOURCE -> $TARGET_DYNAMIC_CORE"
  echo "dry-run: would install $DYNAMIC_SOURCE -> $TARGET_DYNAMIC"
  echo "dry-run: would install $PLUGIN_SOURCE -> $TARGET_PLUGIN"
  echo "dry-run: would enable plugin:"
  echo "dry-run:   HERMES_HOME=$HERMES_HOME $HERMES_BIN plugins enable h4v3-resource-scheduler --no-allow-tool-override"
  exit 0
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUPS=()

install_script() {
  local source="$1"
  local target="$2"
  local tmp="${target}.candidate-${TS}"

  if [[ -f "$target" ]]; then
    local backup="${target}.bak-${TS}"
    cp -p "$target" "$backup"
    BACKUPS+=("$backup")
  fi
  install -m 644 "$source" "$tmp"
  mv -f "$tmp" "$target"
}

install_script "$ADMISSION_SOURCE" "$TARGET_ADMISSION"
# Core must land before the stable wrapper that imports it.
install_script "$DYNAMIC_CORE_SOURCE" "$TARGET_DYNAMIC_CORE"
install_script "$DYNAMIC_SOURCE" "$TARGET_DYNAMIC"

if [[ -d "$TARGET_PLUGIN" ]]; then
  install -d -m 700 "$PLUGIN_BACKUP_ROOT"
  backup="$PLUGIN_BACKUP_ROOT/h4v3-resource-scheduler.bak-${TS}"
  mv "$TARGET_PLUGIN" "$backup"
  BACKUPS+=("$backup")
fi
candidate="${TARGET_PLUGIN}.candidate-${TS}"
install -d -m 700 "$candidate"
install -m 644 "$PLUGIN_SOURCE/plugin.yaml" "$candidate/plugin.yaml"
install -m 644 "$PLUGIN_SOURCE/__init__.py" "$candidate/__init__.py"
mv "$candidate" "$TARGET_PLUGIN"

HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins enable \
  h4v3-resource-scheduler --no-allow-tool-override

for pair in \
  "$ADMISSION_SOURCE|$TARGET_ADMISSION" \
  "$DYNAMIC_CORE_SOURCE|$TARGET_DYNAMIC_CORE" \
  "$DYNAMIC_SOURCE|$TARGET_DYNAMIC" \
  "$PLUGIN_SOURCE/plugin.yaml|$TARGET_PLUGIN/plugin.yaml" \
  "$PLUGIN_SOURCE/__init__.py|$TARGET_PLUGIN/__init__.py"
do
  source="${pair%%|*}"
  target="${pair#*|}"
  source_hash="$(sha256sum "$source" | cut -d' ' -f1)"
  target_hash="$(sha256sum "$target" | cut -d' ' -f1)"
  [[ "$source_hash" == "$target_hash" ]] || {
    echo "post-install hash mismatch: $target" >&2
    exit 1
  }
done

echo "H4V3 resource scheduler installed."
echo "Plugin: $TARGET_PLUGIN"
echo "Helpers: $TARGET_ADMISSION ; $TARGET_DYNAMIC_CORE ; $TARGET_DYNAMIC"
echo "Restart the dispatcher-owning Hermes gateway after updating kanban config."
if [[ ${#BACKUPS[@]} -gt 0 ]]; then
  echo "Backups:"
  printf '  %s\n' "${BACKUPS[@]}"
fi
