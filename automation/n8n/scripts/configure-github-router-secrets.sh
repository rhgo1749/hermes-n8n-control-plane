#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SECRET_DIR="$ROOT/automation/n8n/state/secrets"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
command -v gh >/dev/null || { echo "gh CLI is required" >&2; exit 2; }
CRON_TOKEN="$HERMES_HOME/plugins/hermes-n8n-cron-auth/.n8n-cron-token"
[[ -f "$CRON_TOKEN" ]] || { echo "Hermes cron token missing: $CRON_TOKEN" >&2; exit 2; }
GITHUB_TOKEN="$(gh auth token 2>/dev/null)"
[[ -n "$GITHUB_TOKEN" ]] || { echo "gh auth token is unavailable" >&2; exit 2; }
install -d -m 700 "$SECRET_DIR"
umask 077
printf '%s\n' "$GITHUB_TOKEN" > "$SECRET_DIR/.github-token.tmp"
chmod 600 "$SECRET_DIR/.github-token.tmp"
mv -f "$SECRET_DIR/.github-token.tmp" "$SECRET_DIR/github-token"
install -m 600 "$CRON_TOKEN" "$SECRET_DIR/hermes-cron-token"
if [[ ! -f "$SECRET_DIR/github-webhook-secret" ]]; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' > "$SECRET_DIR/.github-webhook-secret.tmp"
  chmod 600 "$SECRET_DIR/.github-webhook-secret.tmp"
  mv -f "$SECRET_DIR/.github-webhook-secret.tmp" "$SECRET_DIR/github-webhook-secret"
fi
chmod 600 "$SECRET_DIR/github-token" "$SECRET_DIR/hermes-cron-token" "$SECRET_DIR/github-webhook-secret"
unset GITHUB_TOKEN
echo "GitHub router secrets configured under: $SECRET_DIR"
echo "Secret values were not printed."
