#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE="$ROOT/hermes-plugin/kanban-protocol-loop-guard"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-hermes}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install-kanban-protocol-loop-guard.sh [--hermes-home PATH] [--hermes-bin PATH] [--dry-run]

Installs and enables the H4V3 dispatcher safety observer that sticky-blocks a
Kanban task after five consecutive max-runtime timeouts / clean-exit lifecycle
protocol violations. Hermes core remains unchanged. A gateway/dispatcher
restart is required after activation.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home) [[ $# -ge 2 ]] || { echo "--hermes-home requires a path" >&2; exit 2; }; HERMES_HOME="$2"; shift 2 ;;
    --hermes-bin) [[ $# -ge 2 ]] || { echo "--hermes-bin requires a command" >&2; exit 2; }; HERMES_BIN="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for source in "$SOURCE/plugin.yaml" "$SOURCE/__init__.py"; do
  [[ -f "$source" ]] || { echo "plugin source missing: $source" >&2; exit 1; }
done
[[ -d "$HERMES_HOME" ]] || { echo "Hermes home not found: $HERMES_HOME" >&2; exit 2; }
command -v "$HERMES_BIN" >/dev/null 2>&1 || { echo "Hermes binary not found: $HERMES_BIN" >&2; exit 2; }

TARGET_ROOT="$HERMES_HOME/plugins"
TARGET="$TARGET_ROOT/kanban-protocol-loop-guard"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${TARGET}.bak-${TS}"
[[ ! -L "$TARGET" ]] || { echo "refusing to replace symlinked plugin target: $TARGET" >&2; exit 1; }
[[ ! -e "$TARGET" || -d "$TARGET" ]] || { echo "refusing to replace non-directory plugin target: $TARGET" >&2; exit 1; }

if [[ "$DRY_RUN" -eq 1 ]]; then
  python3 -m py_compile "$SOURCE/__init__.py"
  echo "dry-run: validated $SOURCE"
  echo "dry-run: would install $SOURCE -> $TARGET"
  echo "dry-run: would enable kanban-protocol-loop-guard"
  echo "dry-run: gateway/dispatcher restart required after activation"
  exit 0
fi

TMP="$(mktemp -d /tmp/hermes-kanban-loop-guard.XXXXXX)"
CANDIDATE="$TARGET_ROOT/.kanban-protocol-loop-guard.candidate-${TS}"
cleanup() { rm -rf "$CANDIDATE" "$TMP"; }
trap cleanup EXIT
install -d -m 700 "$TARGET_ROOT" "$CANDIDATE"
install -m 644 "$SOURCE/plugin.yaml" "$CANDIDATE/plugin.yaml"
install -m 644 "$SOURCE/__init__.py" "$CANDIDATE/__init__.py"
PYTHONPYCACHEPREFIX="$TMP/pycache" python3 -m py_compile "$CANDIDATE/__init__.py"

if [[ -d "$TARGET" ]]; then mv "$TARGET" "$BACKUP"; fi
mv "$CANDIDATE" "$TARGET"
restore_previous() {
  rm -rf "$TARGET"
  if [[ -d "$BACKUP" ]]; then mv "$BACKUP" "$TARGET"; fi
}
if ! HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins enable kanban-protocol-loop-guard --no-allow-tool-override; then
  restore_previous
  echo "plugin activation failed; previous plugin restored" >&2
  exit 1
fi
cmp -s "$SOURCE/plugin.yaml" "$TARGET/plugin.yaml" || { restore_previous; echo "installed manifest mismatch" >&2; exit 1; }
cmp -s "$SOURCE/__init__.py" "$TARGET/__init__.py" || { restore_previous; echo "installed plugin mismatch" >&2; exit 1; }

echo "Installed and enabled kanban-protocol-loop-guard at $TARGET"
if [[ -d "$BACKUP" ]]; then
  echo "Rollback: rm -rf \"$TARGET\" && mv \"$BACKUP\" \"$TARGET\""
else
  echo "Rollback: HERMES_HOME=\"$HERMES_HOME\" \"$HERMES_BIN\" plugins disable kanban-protocol-loop-guard && rm -rf \"$TARGET\""
fi
echo "Restart the Hermes gateway/dispatcher process to register the updated hooks."
