# Hermes → n8n GitHub agent-ready intake control plane

This repository contains the **scope-limited** migration of one existing
Hermes job—GitHub agent-ready Issue intake—to a self-hosted n8n Community
Edition instance.

```text
Schedule / GitHub event
          │
          ▼
        n8n
          │  existing Hermes dashboard cron route
          ▼
Hermes cron trigger → existing ticker → existing script / Kanban / worker path
          │
          ▼
  n8n pauses the legacy schedule after a bounded cleanup delay
  (host canaries verify actual ticker claim/completion)
```

## Scope

- n8n Community Edition runs persistently on the Ubuntu host through Docker Compose.
- Only `bf431b2a6ba6` (GitHub agent-ready Issue intake) is represented by an inactive, tracked n8n Schedule Trigger export. Optional GitHub Trigger exports only wake that same intake job.
- n8n reuses the existing Hermes dashboard cron **trigger** and **pause** routes for that one job; it does not spawn processes or implement workers. The installer fail-closes if the allowlisted job ID is not unique to `default`.
- The existing GitHub intake script remains authoritative for filtering, idempotency, Kanban projection, and reconciliation. Optional n8n GitHub Trigger workflows only wake that same script.
- The five GitHub event workflows and the temporary polling fallback are production-serialized with `N8N_CONCURRENCY_PRODUCTION_LIMIT=1`. This prevents an older delayed pause from overtaking a newer trigger for the same Hermes intake job. See [GitHub event concurrency](docs/GITHUB_EVENT_CONCURRENCY.md).
- The intake's legacy Hermes schedule is paused only after its n8n canary passes. It is preserved for rollback.
- `168bd63461e7`, `e432a90c1361`, `df360bfa297d`, and `27f6725028ff` remain Hermes-owned. In particular, H4V3 Broadcast Health Monitor has no n8n-native redesign in this scope.

## Explicit non-goals

This change does **not** modify Hermes core, redesign Kanban state, create a new dispatch/completion API, add a separate idempotency database, move H4V3 Broadcast Health Monitor to n8n, or recreate worker/worktree/spawn behavior in n8n. The single production slot is an n8n edge-safety guard for the shared intake job, not a new Hermes concurrency system.

See [operations](docs/OPERATIONS.md) for the controlled host rollout, [GitHub event concurrency](docs/GITHUB_EVENT_CONCURRENCY.md) for the event activation gate, and [future improvements](docs/FUTURE_IMPROVEMENTS.md) for intentionally deferred ideas.

## Repository layout

| Path | Purpose |
|---|---|
| `automation/n8n/compose.yaml` | Private, persistent n8n CE host deployment |
| `automation/n8n/workflows/*.json` | Inactive, credential-free n8n workflow templates tracked in Git |
| `automation/n8n/scripts/` | Host install, service-auth deployment, render/import/export, cutover/rollback, static validation |
| `hermes-plugin/n8n-cron-auth/` | User plugin that token-authenticates only exact existing cron trigger/pause routes |
| `tests/test_n8n_cron_auth_plugin.py` | Runtime test for the route allowlist and secret-file fail-closed behavior |
| `tests/test_github_event_concurrency_contract.py` | Regression contract for the five GitHub event workflows and their single production execution slot |

## Fast host path

> Run these from a checkout on the **Ubuntu host**, not from the Hermes container. The current agent container has no Docker socket, sudo, systemd, or host SSH authority.

```bash
# 1. Docker Engine + Compose v2 must already be installed.
#    This enables Docker boot recovery only when explicitly requested.
automation/n8n/scripts/host-install.sh --enable-docker-service

# 2. Deploy the least-privilege Hermes service token plugin.
#    Restart the existing Hermes dashboard using its current supervisor afterward.
automation/n8n/scripts/configure-hermes-service-auth.sh --hermes-home "$HOME/.hermes"

# 3. Render/import inactive workflows for the current dashboard bind address.
automation/n8n/scripts/import-workflows.sh \
  --dashboard-url http://100.107.12.90:9119

# 4. Follow the manual credential + canary gates in docs/OPERATIONS.md.
# 5. After the sole intake Schedule Trigger workflow has passed its canary and
#    been restored, pause only the legacy intake schedule. Activate that
#    workflow only after this command succeeds (see docs/OPERATIONS.md).
automation/n8n/scripts/cutover.sh --confirm-n8n-verified \
  --hermes-home "$HOME/.hermes"
```

Do not copy the generated token into this repository, n8n workflow JSON, shell history, or chat. n8n stores the Header Auth credential encrypted with the host-specific `N8N_ENCRYPTION_KEY`.

The 75-second pause is intentionally a bounded compensating cleanup, not a completion acknowledgement. A trigger or pause error means the canary is not approved: leave the workflow inactive, restore the legacy job, and follow the recovery gate in `docs/OPERATIONS.md`.

## Local static verification

```bash
python3 automation/n8n/scripts/validate.py
python3 tests/test_github_event_concurrency_contract.py
/ws/hermes-agent/venv/bin/python3 tests/test_n8n_cron_auth_plugin.py
```

The workflows are intentionally **inactive** and contain the literal `__HERMES_DASHBOARD_URL__` placeholder. `import-workflows.sh` renders host-specific copies into ignored `automation/n8n/state/` before import.
