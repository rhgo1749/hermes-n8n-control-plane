#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
N8N_DIR="$ROOT/automation/n8n"
# shellcheck source=state-root.sh
. "$SCRIPT_DIR/state-root.sh"
STATE_ROOT="$(h4v3_n8n_state_root "$N8N_DIR")"
SECRET_DIR="$STATE_ROOT/secrets"

usage() {
  cat <<'EOF'
Usage: configure-github-router-secrets.sh

Stores:
  - GitHub API token
  - stable GitHub webhook HMAC secret
  - stable Hermes intake-control token

No secret value is printed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    # Backward-compatible no-op while older operator notes disappear.
    --hermes-home)
      [[ $# -ge 2 ]] || {
        echo "--hermes-home requires a path" >&2
        exit 2
      }
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

command -v gh >/dev/null || {
  echo "gh CLI is required" >&2
  exit 2
}

GITHUB_TOKEN="$(gh auth token 2>/dev/null)"
[[ -n "$GITHUB_TOKEN" ]] || {
  echo "gh auth token is unavailable" >&2
  exit 2
}

install -d -m 700 "$SECRET_DIR"
umask 077

printf '%s\n' "$GITHUB_TOKEN" \
  > "$SECRET_DIR/.github-token.tmp"
chmod 600 "$SECRET_DIR/.github-token.tmp"
mv -f \
  "$SECRET_DIR/.github-token.tmp" \
  "$SECRET_DIR/github-token"

if [[ ! -f "$SECRET_DIR/github-webhook-secret" ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' \
    > "$SECRET_DIR/.github-webhook-secret.tmp"
  chmod 600 "$SECRET_DIR/.github-webhook-secret.tmp"
  mv -f \
    "$SECRET_DIR/.github-webhook-secret.tmp" \
    "$SECRET_DIR/github-webhook-secret"
fi

if [[ ! -f "$SECRET_DIR/hermes-intake-control-token" ]]; then
  # One-time migration: preserve the existing credential value so persisted
  # n8n HTTP Header Auth credentials and operator clients do not break merely
  # because the token's role/name changes from cron-specific to intake-control.
  if [[ -f "$SECRET_DIR/hermes-cron-token" ]]; then
    install -m 600 \
      "$SECRET_DIR/hermes-cron-token" \
      "$SECRET_DIR/hermes-intake-control-token"
    echo "Migrated existing service credential to intake-control token."
  else
    python3 -c 'import secrets; print(secrets.token_hex(32))' \
      > "$SECRET_DIR/.hermes-intake-control-token.tmp"
    chmod 600 "$SECRET_DIR/.hermes-intake-control-token.tmp"
    mv -f \
      "$SECRET_DIR/.hermes-intake-control-token.tmp" \
      "$SECRET_DIR/hermes-intake-control-token"
  fi
fi

chmod 600 \
  "$SECRET_DIR/github-token" \
  "$SECRET_DIR/github-webhook-secret" \
  "$SECRET_DIR/hermes-intake-control-token"

unset GITHUB_TOKEN

echo "GitHub router/intake secrets configured under: $SECRET_DIR"
echo "Secret values were not printed."
