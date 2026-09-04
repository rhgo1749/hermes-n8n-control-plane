#!/usr/bin/env bash
# Install the dispatcher-side completion safety wake Hermes plugin.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE="$ROOT/hermes-plugin/github-completion-dispatch-safety-wake"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-hermes}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install-github-completion-dispatch-safety-wake.sh [--hermes-home PATH] [--hermes-bin PATH] [--dry-run]

Installs and enables the dispatcher-side bounded safety observer that replays
the existing github-completion-edge-wake observer only when a recent committed
GitHub-backed completion is still stranded in provisional done.

The canonical edge remains the only GitHub/Kanban transition owner. This helper
does not add a cron, polling loop, state database, or direct Kanban write.

A Hermes gateway/dispatcher restart is required after activation so the
long-lived dispatcher process registers the new hook.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hermes-home)
      [[ $# -ge 2 ]] || { echo "--hermes-home requires a path" >&2; exit 2; }
      HERMES_HOME="$2"; shift 2 ;;
    --hermes-bin)
      [[ $# -ge 2 ]] || { echo "--hermes-bin requires a command" >&2; exit 2; }
      HERMES_BIN="$2"; shift 2 ;;
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

PRIMARY="$HERMES_HOME/plugins/github-completion-edge-wake/__init__.py"
EDGE="$HERMES_HOME/scripts/kanban-github-sync.py"
[[ -f "$PRIMARY" && ! -L "$PRIMARY" ]] || {
  echo "primary completion plugin is missing or unsafe: $PRIMARY" >&2
  exit 1
}
[[ -f "$EDGE" && ! -L "$EDGE" ]] || {
  echo "canonical edge runtime is missing or unsafe: $EDGE" >&2
  exit 1
}

TARGET_ROOT="$HERMES_HOME/plugins"
TARGET="$TARGET_ROOT/github-completion-dispatch-safety-wake"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="${TARGET}.bak-${TS}"

if [[ -L "$TARGET" ]]; then
  echo "refusing to replace symlinked plugin target: $TARGET" >&2
  exit 1
fi
if [[ -e "$TARGET" && ! -d "$TARGET" ]]; then
  echo "refusing to replace non-directory plugin target: $TARGET" >&2
  exit 1
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  python3 -m py_compile "$SOURCE/__init__.py"
  echo "dry-run: validated $SOURCE"
  echo "dry-run: would install $SOURCE -> $TARGET"
  echo "dry-run: would enable github-completion-dispatch-safety-wake"
  echo "dry-run: gateway/dispatcher restart required after activation"
  exit 0
fi

TMP="$(mktemp -d /tmp/hermes-completion-dispatch-safety.XXXXXX)"
CANDIDATE="$TARGET_ROOT/.github-completion-dispatch-safety-wake.candidate-${TS}"
cleanup() {
  rm -rf "$CANDIDATE" "$TMP"
}
trap cleanup EXIT

install -d -m 700 "$TARGET_ROOT" "$CANDIDATE"
install -m 644 "$SOURCE/plugin.yaml" "$CANDIDATE/plugin.yaml"
install -m 644 "$SOURCE/__init__.py" "$CANDIDATE/__init__.py"
PYTHONPYCACHEPREFIX="$TMP/pycache" python3 -m py_compile "$CANDIDATE/__init__.py"

if [[ -d "$TARGET" ]]; then
  mv "$TARGET" "$BACKUP"
fi
mv "$CANDIDATE" "$TARGET"

restore_previous() {
  rm -rf "$TARGET"
  if [[ -d "$BACKUP" ]]; then
    mv "$BACKUP" "$TARGET"
  fi
}

if ! HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins enable \
  github-completion-dispatch-safety-wake --no-allow-tool-override
then
  restore_previous
  echo "plugin activation failed; previous plugin restored" >&2
  exit 1
fi

cmp -s "$SOURCE/plugin.yaml" "$TARGET/plugin.yaml" || {
  restore_previous
  echo "installed manifest mismatch" >&2
  exit 1
}
cmp -s "$SOURCE/__init__.py" "$TARGET/__init__.py" || {
  restore_previous
  echo "installed plugin mismatch" >&2
  exit 1
}

echo "Installed and enabled github-completion-dispatch-safety-wake at $TARGET"
if [[ -d "$BACKUP" ]]; then
  echo "Rollback: rm -rf \"$TARGET\" && mv \"$BACKUP\" \"$TARGET\""
else
  echo "Rollback: HERMES_HOME=\"$HERMES_HOME\" \"$HERMES_BIN\" plugins disable github-completion-dispatch-safety-wake && rm -rf \"$TARGET\""
fi
echo "Restart the Hermes gateway/dispatcher process to register the new hook."
