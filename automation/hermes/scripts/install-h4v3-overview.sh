#!/usr/bin/env bash
# Deploy the read-only H4V3 Overview dashboard plugin to an existing Hermes home.
# Run this on the host as the account that owns the Hermes installation.
# Independent from configure-hermes-service-auth.sh: the n8n service-auth
# installer no longer forces the Overview onto the host.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SOURCE="$ROOT/hermes-plugin/h4v3-overview"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="${HERMES_BIN:-hermes}"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install-h4v3-overview.sh [--hermes-home PATH] [--hermes-bin PATH] [--dry-run]

Deploys hermes-plugin/h4v3-overview/ into $HERMES_HOME/plugins/h4v3-overview/
and enables it (no tool-override permission). Files are replaced atomically
(install to the target path after a same-directory candidate copy) and the
previous plugin directory is kept as a timestamped backup for rollback.

A dashboard restart is required after installation so the plugin tab
(/h4v3-overview) registers. Rollback:
  mv "$HERMES_HOME/plugin-backups/h4v3-overview/h4v3-overview.bak-<ts>" "$HERMES_HOME/plugins/h4v3-overview"
  hermes plugins enable h4v3-overview --no-allow-tool-override
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

[[ -f "$SOURCE/plugin.yaml" && -f "$SOURCE/__init__.py" && -f "$SOURCE/dashboard/manifest.json" ]] || {
  echo "H4V3 Overview source missing: $SOURCE" >&2; exit 1;
}
[[ -d "$HERMES_HOME" ]] || { echo "Hermes home not found: $HERMES_HOME" >&2; exit 2; }

# Candidate validation: the backend must compile with the host python3 before
# anything is written (the dashboard imports it as a module on restart).
python3 -m py_compile "$SOURCE/dashboard/plugin_api.py" "$SOURCE/__init__.py" || {
  echo "candidate validation failed (py_compile)" >&2; exit 1;
}

TARGET="$HERMES_HOME/plugins/h4v3-overview"
BACKUP_ROOT="$HERMES_HOME/plugin-backups/h4v3-overview"
FILES=(
  "plugin.yaml"
  "__init__.py"
  "dashboard/manifest.json"
  "dashboard/plugin_api.py"
  "dashboard/dist/index.js"
  "dashboard/dist/style.css"
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: would install to $TARGET"
  for file in "${FILES[@]}"; do
    echo "dry-run:   $file -> $TARGET/$file"
  done
  echo "dry-run:   HERMES_HOME=$HERMES_HOME $HERMES_BIN plugins enable h4v3-overview --no-allow-tool-override"
  exit 0
fi

# Atomic, rollback-safe install: keep the previous plugin directory as a
# timestamped backup, write the candidate into a temp dir on the same
# filesystem, then mv the whole directory into place.
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$BACKUP_ROOT/h4v3-overview.bak-${TS}"
if [[ -d "$TARGET" ]]; then
  install -d -m 700 "$BACKUP_ROOT"
  mv "$TARGET" "$BACKUP"
fi

CANDIDATE="${TARGET}.candidate-${TS}"
install -d -m 700 "$CANDIDATE/dashboard/dist"
for file in "${FILES[@]}"; do
  install -m 644 "$SOURCE/$file" "$CANDIDATE/$file"
done
mv "$CANDIDATE" "$TARGET"

HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" plugins enable h4v3-overview --no-allow-tool-override

echo "H4V3 Overview installed at: $TARGET"
echo "Previous version (rollback): $BACKUP (restore with mv, then re-enable)"
echo "Restart the existing Hermes dashboard with its current supervisor to load the plugin tab."
