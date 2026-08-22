#!/usr/bin/env bash
# Shared host-side config/state path resolvers for the n8n control plane.
# Source this file; do not execute it directly.

h4v3_n8n_operator_home() {
  local value="${HOME:?HOME is required}"

  # Root-only installers are normally entered via sudo. Resolve the invoking
  # operator's home rather than silently creating config/state under /root.
  if [[ "${EUID:-$(id -u)}" -eq 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    value="$(python3 - "${SUDO_USER}" <<'PY'
import pwd
import sys
print(pwd.getpwnam(sys.argv[1]).pw_dir)
PY
)"
  fi

  printf '%s\n' "$value"
}

h4v3_n8n_env_file() {
  local n8n_dir="${1:?n8n directory is required}"
  local value="${HERMES_N8N_ENV_FILE:-}"
  local operator_home
  operator_home="$(h4v3_n8n_operator_home)"

  if [[ -z "$value" ]]; then
    if [[ "${EUID:-$(id -u)}" -eq 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
      value="$operator_home/.config/hermes-n8n-control-plane/n8n.env"
    else
      value="${XDG_CONFIG_HOME:-$operator_home/.config}/hermes-n8n-control-plane/n8n.env"
    fi
  fi

  case "$value" in
    /*) ;;
    *)
      echo "HERMES_N8N_ENV_FILE must be an absolute host path: $value" >&2
      return 2
      ;;
  esac

  printf '%s\n' "$value"
}

h4v3_n8n_state_root() {
  local n8n_dir="${1:?n8n directory is required}"
  local env_file
  env_file="$(h4v3_n8n_env_file "$n8n_dir")"
  local value="${HERMES_N8N_STATE_ROOT:-}"
  local operator_home
  operator_home="$(h4v3_n8n_operator_home)"

  if [[ -z "$value" && -f "$env_file" ]]; then
    value="$(python3 - "$env_file" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
for raw in path.read_text(encoding="utf-8").splitlines():
    if not raw or raw.lstrip().startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    if key.strip() != "HERMES_N8N_STATE_ROOT":
        continue
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(value)
    break
PY
)"
  fi

  if [[ -z "$value" ]]; then
    if [[ "${EUID:-$(id -u)}" -eq 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
      value="$operator_home/.local/state/hermes-n8n-control-plane"
    else
      value="${XDG_STATE_HOME:-$operator_home/.local/state}/hermes-n8n-control-plane"
    fi
  fi

  case "$value" in
    /*) ;;
    *)
      echo "HERMES_N8N_STATE_ROOT must be an absolute host path: $value" >&2
      return 2
      ;;
  esac

  printf '%s\n' "$value"
}
