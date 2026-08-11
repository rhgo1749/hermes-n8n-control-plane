# Hermes → n8n schedule and GitHub automation migration

This repository contains the **scope-limited** migration from Hermes-owned cron glue to a self-hosted n8n Community Edition instance.

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
- Existing active Hermes script-only cron jobs are represented as inactive, tracked n8n Schedule Trigger exports.
- n8n reuses the existing Hermes dashboard cron **trigger** and **pause** routes; it does not spawn processes or implement workers. The installer fail-closes if an allowlisted job ID is not unique to its intended profile.
- The existing GitHub intake script remains authoritative for filtering, idempotency, Kanban projection, and reconciliation. Optional n8n GitHub Trigger workflows only wake that same script.
- Legacy Hermes schedule entries are paused only after n8n canaries pass. They are preserved for rollback.

## Explicit non-goals

This change does **not** modify Hermes core, redesign Kanban state, create a new dispatch/completion API, add a separate idempotency database, move concurrency policy to n8n, or recreate worker/worktree/spawn behavior in n8n.

See [operations](docs/OPERATIONS.md) for the controlled host rollout and [future improvements](docs/FUTURE_IMPROVEMENTS.md) for intentionally deferred ideas.

## Repository layout

| Path | Purpose |
|---|---|
| `automation/n8n/compose.yaml` | Private, persistent n8n CE host deployment |
| `automation/n8n/workflows/*.json` | Inactive, credential-free n8n workflow templates tracked in Git |
| `automation/n8n/scripts/` | Host install, service-auth deployment, render/import/export, cutover/rollback, static validation |
| `hermes-plugin/n8n-cron-auth/` | User plugin that token-authenticates only exact existing cron trigger/pause routes |
| `tests/test_n8n_cron_auth_plugin.py` | Runtime test for the route allowlist and secret-file fail-closed behavior |

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
# 5. After every schedule workflow has passed its canary and been restored,
#    pause the legacy schedules. Activate Schedule Trigger workflows only after
#    this command succeeds (see docs/OPERATIONS.md).
automation/n8n/scripts/cutover.sh --confirm-n8n-verified \
  --hermes-home "$HOME/.hermes"
```

Do not copy the generated token into this repository, n8n workflow JSON, shell history, or chat. n8n stores the Header Auth credential encrypted with the host-specific `N8N_ENCRYPTION_KEY`.

The 75-second pause is intentionally a bounded compensating cleanup, not a completion acknowledgement. A trigger or pause error means the canary is not approved: leave the workflow inactive, restore the legacy job, and follow the recovery gate in `docs/OPERATIONS.md`.

## Local static verification

```bash
python3 automation/n8n/scripts/validate.py
/ws/hermes-agent/venv/bin/python3 tests/test_n8n_cron_auth_plugin.py
```

The workflows are intentionally **inactive** and contain the literal `__HERMES_DASHBOARD_URL__` placeholder. `import-workflows.sh` renders host-specific copies into ignored `automation/n8n/state/` before import.
