# Hermes → n8n GitHub agent-ready intake control plane

External H4V3 Hermes control plane for automation, edge reconciliation,
operator notifications, and an operator-facing read-only Overview — without
modifying Hermes core.

This repository contains the **scope-limited** migration of one existing
Hermes job — GitHub agent-ready Issue intake — to a self-hosted n8n Community
Edition instance, plus the edge reconciliation that keeps GitHub-backed Kanban
cards in sync and the operator surfaces (Telegram policy + H4V3 Overview) that
make the boards actionable.

```text
Schedule / GitHub event
          │
          ▼
        n8n
          │  existing Hermes dashboard cron route
          ▼
Hermes cron trigger → existing ticker → existing script / Kanban / worker path
          │                              │
          ▼                              ▼
  n8n pauses the legacy schedule   edge reconciliation (GitHub ↔ Kanban)
  after a bounded cleanup delay          │
  (host canaries verify)                 ├── Telegram (human-attention only)
                                         └── H4V3 Overview (dashboard, read-only)
```

## Scope

- n8n Community Edition runs persistently on the Ubuntu host through Docker Compose.
- Only `bf431b2a6ba6` (GitHub agent-ready Issue intake) is represented by an inactive, tracked n8n Schedule Trigger export. Optional GitHub Trigger exports only wake that same intake job.
- n8n reuses the existing Hermes dashboard cron **trigger** and **pause** routes for that one job; it does not spawn processes or implement workers. The installer fail-closes if the allowlisted job ID is not unique to `default`.
- The existing GitHub intake script remains authoritative for filtering, idempotency, Kanban projection, and reconciliation. Optional n8n GitHub Trigger workflows only wake that same script.
- The five GitHub event workflows and the temporary polling fallback are production-serialized with `N8N_CONCURRENCY_PRODUCTION_LIMIT=1`. This prevents an older delayed pause from overtaking a newer trigger for the same Hermes intake job. See [GitHub event concurrency](docs/GITHUB_EVENT_CONCURRENCY.md).
- The intake's legacy Hermes schedule is paused only after its n8n canary passes. It is preserved for rollback.
- `168bd63461e7`, `e432a90c1361`, `df360bfa297d`, and `27f6725028ff` remain Hermes-owned. In particular, H4V3 Broadcast Health Monitor has no n8n-native redesign in this scope.
- **Operator notifications**: the intake formats and sends Telegram alerts through the existing Hermes messaging path (`hermes send`). Only human-attention incidents are sent; routine lifecycle transitions are suppressed. See [docs/H4V3_OVERVIEW.md](docs/H4V3_OVERVIEW.md).
- **H4V3 Overview**: a read-only multi-board Hermes dashboard plugin (`hermes-plugin/h4v3-overview/`) that projects every Kanban board onto one screen (Need You / Blocked / Review / Running / Ready, rework counts, provenance). See [docs/H4V3_OVERVIEW.md](docs/H4V3_OVERVIEW.md).

## Explicit non-goals

This change does **not** modify Hermes core, redesign Kanban state, create a new dispatch/completion API, add a separate idempotency database, move H4V3 Broadcast Health Monitor to n8n, or recreate worker/worktree/spawn behavior in n8n. The single production slot is an n8n edge-safety guard for the shared intake job, not a new Hermes concurrency system. The H4V3 Overview is a read-only projection: no task editing, worker control, analytics, Issue/PR creation, new auth, or public exposure.

See [operations](docs/OPERATIONS.md) for the controlled host rollout, [GitHub event concurrency](docs/GITHUB_EVENT_CONCURRENCY.md) for the event activation gate, and [future improvements](docs/FUTURE_IMPROVEMENTS.md) for intentionally deferred ideas.

## Repository layout

| Path | Purpose |
|---|---|
| `automation/n8n/compose.yaml` | Private, persistent n8n CE host deployment |
| `automation/n8n/workflows/*.json` | Inactive, credential-free n8n workflow templates tracked in Git |
| `automation/n8n/scripts/` | Host install, service-auth deployment, render/import/export, cutover/rollback, static validation |
| `automation/hermes/scripts/github-agent-ready-kanban-intake.py` | Authoritative GitHub intake + reconciliation tick and Telegram notification policy |
| `automation/hermes/scripts/install-h4v3-overview.sh` | Optional standalone Overview dashboard plugin installer (candidate copy, validation, atomic replace, rollback) |
| `automation/hermes/scripts/deploy-intake-edge.sh` | Safe host deploy of intake/edge runtime copies (candidate copy, validation, atomic replace, rollback; never touches cron) |
| `edge/kanban-github-sync.py` | GitHub ↔ Kanban edge reconciliation (completion, rework lifecycle, human-attention evidence) |
| `hermes-plugin/n8n-cron-auth/` | User plugin that token-authenticates only exact existing cron trigger/pause routes |
| `hermes-plugin/h4v3-overview/` | Read-only H4V3 Overview dashboard plugin (multi-board projection) |
| `docs/H4V3_OVERVIEW.md` | Overview responsibilities, Need You rules, Telegram suppress/send matrix, install/rollback |
| `tests/test_n8n_cron_auth_plugin.py` | Runtime test for the route allowlist and secret-file fail-closed behavior |
| `tests/test_github_event_concurrency_contract.py` | Regression contract for the five GitHub event workflows and their single production execution slot |
| `tests/test_h4v3_overview.py`, `tests/test_h4v3_notification_policy.py` | Overview projection + notification policy regression tests |

## Fast host path

> Run these from a checkout on the **Ubuntu host**, not from the Hermes container. The current agent container has no Docker socket, sudo, systemd, or host SSH authority.

```bash
# 1. Docker Engine + Compose v2 must already be installed.
#    This enables Docker boot recovery only when explicitly requested.
automation/n8n/scripts/host-install.sh --enable-docker-service

# 2. Deploy the least-privilege Hermes service token plugin.
#    Restart the existing Hermes dashboard using its current supervisor afterward.
automation/n8n/scripts/configure-hermes-service-auth.sh --hermes-home "$HOME/.hermes"

# 2b. Optional: install the read-only H4V3 Overview dashboard plugin
#     (independent of service-auth; restart the dashboard afterward).
automation/hermes/scripts/install-h4v3-overview.sh --hermes-home "$HOME/.hermes"

# 2c. Optional: deploy the intake/edge runtime scripts (see docs/H4V3_OVERVIEW.md —
#     the live cron executes deployed copies under $HERMES_HOME/scripts, not this checkout).
automation/hermes/scripts/deploy-intake-edge.sh --hermes-home "$HOME/.hermes"

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
python3 tests/test_repo_scoped_intake.py
python3 tests/test_h4v3_overview.py
python3 tests/test_h4v3_notification_policy.py
/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py
```

The workflows are intentionally **inactive** and contain the literal `__HERMES_DASHBOARD_URL__` placeholder. `import-workflows.sh` renders host-specific copies into ignored `automation/n8n/state/` before import.
