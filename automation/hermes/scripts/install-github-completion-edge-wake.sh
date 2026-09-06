#!/usr/bin/env bash
# Install the completion-side GitHub edge-wake Hermes plugin.
# Run this manually in the namespace that owns the active Hermes runtime.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE="$ROOT/hermes-plugin/github-completion-edge-wake"
TIMEOUT_SOURCE="$ROOT/automation/hermes/edge_sync_timeout.py"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-hermes}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install-github-completion-edge-wake.sh [--hermes-home PATH] [--hermes-bin PATH] [--dry-run]

Installs the repository-owned completion observer into
$HERMES_HOME/plugins/github-completion-edge-wake/ using a candidate copy and
an atomic directory switch. The previous directory is retained as a
.timestamped backup for rollback. The plugin is enabled after the switch;
a Hermes worker/dashboard restart is still a separate operator gate.

The observer only wakes the already-deployed
$HERMES_HOME/scripts/kanban-github-sync.py edge owner for GitHub-backed
post-completion rows. It does not modify Hermes core, Kanban schema, jobs, or
n8n state.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home)
      [[ $# -ge 2 ]] || { echo "--hermes-home requires a path" >&2; exit 2; }
      HERMES_HOME="$2"
      shift 2
      ;;
    --hermes-bin)
      [[ $# -ge 2 ]] || { echo "--hermes-bin requires a command" >&2; exit 2; }
      HERMES_BIN="$2"
      shift 2
      ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for source in "$SOURCE/plugin.yaml" "$SOURCE/__init__.py" "$TIMEOUT_SOURCE"; do
  [[ -f "$source" ]] || { echo "plugin source missing: $source" >&2; exit 1; }
done
[[ -d "$HERMES_HOME" ]] || { echo "Hermes home not found: $HERMES_HOME" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 2; }
command -v install >/dev/null 2>&1 || { echo "install is required" >&2; exit 2; }
command -v cmp >/dev/null 2>&1 || { echo "cmp is required" >&2; exit 2; }
command -v "$HERMES_BIN" >/dev/null 2>&1 || { echo "Hermes binary not found: $HERMES_BIN" >&2; exit 2; }

TARGET_ROOT="$HERMES_HOME/plugins"
TARGET="$TARGET_ROOT/github-completion-edge-wake"
BACKUP_ROOT="$HERMES_HOME/plugin-backups/github-completion-edge-wake"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_ROOT/github-completion-edge-wake.bak-${TS}"

if [[ -L "$TARGET" ]]; then
  echo "refusing to replace symlinked plugin target: $TARGET" >&2
  exit 1
fi
if [[ -e "$TARGET" && ! -d "$TARGET" ]]; then
  echo "refusing to replace non-directory plugin target: $TARGET" >&2
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: would install $SOURCE -> $TARGET"
  echo "dry-run: would run HERMES_HOME=$HERMES_HOME $HERMES_BIN plugins enable github-completion-edge-wake --no-allow-tool-override"
  echo "dry-run: restart the worker/dashboard supervisor after activation"
  if [[ -d "$TARGET" ]]; then
    echo "dry-run: rollback backup would be $BACKUP"
  else
    echo "dry-run: rollback would remove $TARGET and disable the plugin"
  fi
  exit 0
fi

TMP="$(mktemp -d /tmp/hermes-completion-edge-wake.XXXXXX)"
CANDIDATE="$TARGET_ROOT/.github-completion-edge-wake.candidate-${TS}"
if [[ -e "$CANDIDATE" || -L "$CANDIDATE" || -e "$BACKUP" || -L "$BACKUP" ]]; then
  echo "timestamp collision; refusing to overwrite candidate or backup" >&2
  exit 1
fi
cleanup() {
  rm -rf "$CANDIDATE" "$TMP"
}
trap cleanup EXIT

install -d -m 700 "$TARGET_ROOT" "$BACKUP_ROOT" "$CANDIDATE"
install -m 644 "$SOURCE/plugin.yaml" "$CANDIDATE/plugin.yaml"
install -m 644 "$SOURCE/__init__.py" "$CANDIDATE/__init__.py"
install -m 644 "$TIMEOUT_SOURCE" "$CANDIDATE/edge_sync_timeout.py"
PYTHONPYCACHEPREFIX="$TMP/pycache" python3 -m py_compile \
  "$CANDIDATE/__init__.py" "$CANDIDATE/edge_sync_timeout.py"

if [[ -d "$TARGET" ]]; then
  mv "$TARGET" "$BACKUP"
fi
mv "$CANDIDATE" "$TARGET"

restore_previous() {
  if [[ -e "$TARGET" || -L "$TARGET" ]]; then
    rm -rf "$TARGET"
  fi
  if [[ -d "$BACKUP" ]]; then
    mv "$BACKUP" "$TARGET"
  fi
}

if ! HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins enable github-completion-edge-wake --no-allow-tool-override; then
  restore_previous
  echo "plugin activation failed; previous plugin directory restored" >&2
  exit 1
fi

cmp -s "$SOURCE/plugin.yaml" "$TARGET/plugin.yaml" || {
  echo "installed manifest mismatch" >&2
  restore_previous
  exit 1
}
cmp -s "$SOURCE/__init__.py" "$TARGET/__init__.py" || {
  echo "installed plugin mismatch" >&2
  restore_previous
  exit 1
}

cmp -s "$TIMEOUT_SOURCE" "$TARGET/edge_sync_timeout.py" || {
  echo "installed timeout contract mismatch" >&2
  restore_previous
  exit 1
}

# The edge runtime itself is deployed separately by deploy-intake-edge.sh.
# Refuse to claim a complete wake installation when its fixed live path is
# absent; the plugin remains installed so the operator can repair/deploy edge
# and rerun the activation gate without guessing a different path.
EDGE="$HERMES_HOME/scripts/kanban-github-sync.py"
if [[ ! -f "$EDGE" || -L "$EDGE" ]]; then
  echo "WARNING: fixed live edge path is not a regular file: $EDGE" >&2
  echo "The plugin is installed/enabled but will fail closed until edge deployment is complete." >&2
fi

echo "Installed and enabled github-completion-edge-wake at $TARGET"
if [[ -d "$BACKUP" ]]; then
  echo "Rollback: rm -rf \"$TARGET\" && mv \"$BACKUP\" \"$TARGET\""
  echo "Rollback activation: HERMES_HOME=$HERMES_HOME $HERMES_BIN plugins enable github-completion-edge-wake --no-allow-tool-override"
else
  echo "Rollback: rm -rf \"$TARGET\""
  echo "Rollback activation: HERMES_HOME=$HERMES_HOME $HERMES_BIN plugins disable github-completion-edge-wake"
fi
echo "Restart the existing Hermes worker/dashboard supervisor to load the hook."
