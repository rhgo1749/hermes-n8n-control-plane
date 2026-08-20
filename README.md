# Hermes → GitHub event control plane

External H4V3 Hermes control plane for GitHub event intake, edge reconciliation,
operator notifications, and the read-only H4V3 Overview — without modifying
Hermes core.

The GitHub agent-ready intake is now **event-driven**. The existing Hermes cron
job remains the durable execution primitive; n8n no longer owns a five-minute
Schedule Trigger for it.

```text
GitHub repository event
          │
          ▼
   github-router :5681
     HMAC verify
     delivery dedupe (X-GitHub-Delivery)
     enqueue repo scope
          │
          ▼
 lease-controller :5680
          │
          ▼
existing Hermes cron trigger
 default:bf431b2a6ba6
          │
          ▼
existing ticker → existing intake script → Kanban / workers / edge
          │
          └─ latest lease pauses the same Hermes job again
```

## Durable ownership boundary

The migration does **not** recreate or replace the Hermes job.

`default:bf431b2a6ba6` remains the authoritative GitHub intake job. Its stored
job ID, name, script, schedule expression, profile, and Hermes ownership stay
intact. In normal event-driven operation the job is kept **paused between
external wakes**. `lease-controller` calls the existing dashboard
`trigger`/`pause` routes for that exact job.

Do not delete, recreate, rename, or edit the stored Hermes job as part of the
n8n/event migration.

## Current runtime topology

- `github-router` discovers repositories carrying the `hermes-agent` topic,
  validates signed GitHub webhook events, enqueues repository-scoped wake
  records, and asks `lease-controller` to wake Hermes.
- `lease-controller` is bound to `default:bf431b2a6ba6` and prevents stale
  delayed pauses from overtaking a newer trigger.
- `github-agent-ready-kanban-intake.py` remains authoritative for repository
  filtering, idempotency, Kanban projection, and reconciliation. The deployed
  historical live name is a small completion-contract entrypoint backed by the
  canonical implementation installed beside it as
  `github-agent-ready-kanban-intake-core.py`.
- `edge/kanban-github-sync.py` remains authoritative for GitHub ↔ Kanban edge
  lifecycle reconciliation.
- n8n CE remains a private, persistent control-plane service, but **there is no
  tracked n8n GitHub intake Schedule workflow and no tracked per-repository
  GitHub Trigger workflow**.
- `N8N_CONCURRENCY_PRODUCTION_LIMIT=1` is only a retained load limit; intake
  correctness comes from the router scope queue and persisted lease guard.

## Webhook reconciliation

Webhook registration is topic-driven but no longer piggybacks on a five-minute
polling fallback. Reconcile intentionally when onboarding/removing a
`hermes-agent` repository or after webhook configuration changes:

```bash
automation/n8n/scripts/reconcile-github-router.sh
```

The router's authenticated `/fallback` endpoint remains available for deliberate
operator recovery/full-registry intake. Nothing in the tracked n8n workflow set
calls it periodically.

## Scope

- n8n Community Edition runs persistently on the Ubuntu host through Docker
  Compose and stays loopback-only.
- `lease-controller` and `github-router` run as hardened, read-only companion
  services on host networking.
- `hermes-plugin/n8n-cron-auth/` authorizes only trigger/pause for
  `default:bf431b2a6ba6`; it cannot list/create/edit/delete jobs or trigger any
  other cron job.
- Repository membership is discovered from the GitHub topic `hermes-agent`.
- Operator notifications use the existing Hermes messaging path (`hermes send`)
  and only surface human-attention incidents.
- H4V3 Overview is a read-only multi-board dashboard projection.

The following Hermes jobs remain outside this migration and retain their
existing ownership/state: `168bd63461e7`, `e432a90c1361`, `df360bfa297d`,
`27f6725028ff`, and any other non-intake jobs.

## Explicit non-goals

This repository does **not** modify Hermes core, redesign Kanban state, create a
new dispatch/completion API, add a separate idempotency database, recreate
worker/worktree/spawn behavior in n8n, or migrate H4V3 Broadcast Health Monitor
to n8n.

It also does not automatically create a polling schedule as a fallback. A
future periodic fallback/reconciliation policy requires an explicit separately
reviewed decision.

## Repository layout

| Path | Purpose |
|---|---|
| `automation/n8n/compose.yaml` | Private n8n + router + lease-controller deployment |
| `automation/n8n/github-router/router.py` | Signed GitHub event ingress + scope queue + webhook reconciliation |
| `automation/n8n/lease-controller/controller.py` | Existing Hermes job trigger/pause lease guard |
| `automation/n8n/scripts/repository_registry.py` | `hermes-agent` repository discovery and board/checkout authority |
| `automation/n8n/scripts/reconcile-github-router.sh` | Explicit webhook-registry reconciliation |
| `automation/n8n/scripts/import-workflows.sh` | Compatibility no-op/status path; never recreates the retired schedule |
| `automation/hermes/scripts/github-agent-ready-kanban-intake.py` | Canonical GitHub intake + reconciliation tick |
| `automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py` | Live-name wrapper that keeps GitHub-backed worker termination on core `kanban_complete` |
| `automation/hermes/scripts/deploy-intake-edge.sh` | Safe deployment of live intake/edge runtime copies; never changes cron |
| `edge/kanban-github-sync.py` | GitHub ↔ Kanban edge reconciliation |
| `hermes-plugin/n8n-cron-auth/` | Legacy Hermes service authentication plugin for token-protected routes |
| `hermes-plugin/h4v3-overview/` | Read-only multi-board dashboard plugin |
| `docs/OPERATIONS.md` | Host rollout and async-only operating contract |
| `docs/GITHUB_EVENT_CONCURRENCY.md` | Event/lease concurrency contract |
| `docs/GITHUB_COMPLETION_LIFECYCLE.md` | Worker terminal action vs GitHub review/done projection contract |
| `docs/REPOSITORY_REGISTRY.md` | Repository discovery/authority contract |

## Host setup

Run host operations from the Ubuntu host, not from the Hermes worker container.
The worker container has no Docker socket, sudo, systemd, or host SSH authority.

```bash
# 1. Install/start the private control-plane stack.
automation/n8n/scripts/host-install.sh --enable-docker-service

# 2. Install the least-privilege Hermes service-token plugin.
automation/n8n/scripts/configure-hermes-service-auth.sh \
  --hermes-home "$HOME/.hermes"

# Restart the existing Hermes dashboard through its current supervisor.

# 3. Copy GitHub/Hermes secrets for the router.
automation/n8n/scripts/configure-github-router-secrets.sh \
  --hermes-home "$HOME/.hermes"

# 4. Configure the reviewed public HTTPS router URL in .env, restart services,
#    then reconcile topic-managed repository webhooks.
automation/n8n/scripts/reconcile-github-router.sh
```

If an older persisted n8n workflow named
`Hermes schedule · GitHub agent-ready Issue intake` exists in the n8n database,
leave it inactive or delete that n8n workflow record. Do **not** activate it.
The tracked repository no longer contains that Schedule Trigger template.

## Runtime validation

For the live host, verify all of the following:

- `docker compose ps` reports the n8n, lease-controller, and github-router
  services healthy;
- `http://127.0.0.1:5680/healthz` and `http://127.0.0.1:5681/healthz` succeed;
- webhook reconciliation reports the intended `hermes-agent` repositories;
- a signed GitHub test event reaches the router and produces one scoped intake
  wake;
- `default:bf431b2a6ba6` gets a fresh successful run and returns to paused state
  after the current lease cleanup;
- no n8n Schedule Trigger is active for GitHub intake.

## Local deterministic verification

```bash
python3 automation/n8n/scripts/validate.py
python3 tests/test_github_event_concurrency_contract.py
python3 tests/test_github_router.py
python3 tests/test_intake_lease_controller.py
/ws/hermes-agent/venv/bin/python3 tests/test_n8n_cron_auth_plugin.py
/ws/hermes-agent/venv/bin/python3 tests/test_hermes_cron_trigger_pause.py
python3 tests/test_repo_scoped_intake.py
python3 tests/test_intake_completion_contract_entrypoint.py
python3 tests/test_repository_registry.py
python3 tests/test_h4v3_overview.py
python3 tests/test_h4v3_notification_policy.py
/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py
```

GitHub Actions are intentionally not the required validation surface for this
repository; see `AGENTS.md`.
