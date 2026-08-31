# Operations runbook — async-only GitHub intake

## Issue #87 onboarding diagnostics

The first supported GitHub App delivery is acknowledged by the router and
placed in the existing durable scope queue. The existing intake authority then
revalidates the owner, archive state, `hermes-agent` topic, default branch, and
contract visibility before reading registry board intent or touching
`/ws/projects/<repository-name.casefold()>`.
It never relies on a manual webhook reconciliation for first discovery.

Run the read-only diagnostic from the repository root when investigating a
canary or a partial onboarding:

```bash
automation/n8n/scripts/diagnose-github-onboarding.sh --hermes-home "$HOME/.hermes"
```

The command verifies that `default:bf431b2a6ba6` exists exactly once in the
`default` profile, matches the preserved name/script/profile/schedule/lifecycle/
`no_agent`/delivery contract, and that the deployed intake wrapper/core expose
all required onboarding entrypoints. It also checks the loopback router,
lease-controller, and intake-actuator health endpoints.
It does not create, edit, pause, or trigger a Hermes job. `--skip-network`
checks only the local job/script boundary.

Interpret bounded failures as follows: `invalid_signature` or
`authorization_required` means the protected ingress boundary rejected the
request; `repository_not_opted_in`, `repository_archived`,
`owner_scope_mismatch`, or `contract_visibility_invalid` means the fresh
GitHub metadata gate rejected the repository; `checkout_path_conflict`,
`checkout_origin_mismatch`, `checkout_dirty`, `repository_lock_busy`, or
`clone_failed` means no existing checkout was overwritten and only the
current attempt's temporary path is eligible for cleanup. A newly registered
checkout is intentionally retained when a later registry or board step fails;
rerun the operator recovery after fixing the reported boundary. A missing
authoritative job is an operator stop, not permission to create a replacement.

GitHub App installation, permissions, public HTTPS/Funnel routing, protected
secret provisioning, host `/ws/projects` ownership, production canary, and
post-merge edge reconciliation are host-owned validation gates. They are not
proven by repository-local tests.

## 1. Durable Hermes job ownership

The GitHub agent-ready intake continues to execute through the existing Hermes
cron job:

| Profile | Hermes job ID | Script | Stored schedule | Runtime policy |
|---|---|---|---|---|
| `default` | `bf431b2a6ba6` | `github-agent-ready-kanban-intake.py` | existing `*/5 * * * *` definition | preserve definition; normally paused between external wakes |

The stored schedule is historical/durable job metadata. **Do not delete,
recreate, rename, or edit the job or its stored schedule** as part of this
migration. Async-only operation is achieved by keeping the existing job paused
between event-driven `trigger`/`pause` calls.

Other Hermes jobs remain outside this migration and must not be mutated.

## 2. Production event path

```text
GitHub signed webhook
  -> public HTTPS ingress
  -> github-router 127.0.0.1:5681
       -> repository/topic check
       -> HMAC + delivery dedupe
       -> pull_request close/rework -> n8n Webhook 127.0.0.1:5678
            -> allowlisted normalized event
            -> fixed edge actuator 127.0.0.1:5682
                 -> kanban-github-sync.py --board <slug> --json
       -> other intake event -> durable FIFO scope queue
            -> lease-controller 127.0.0.1:5680
                 -> existing Hermes trigger route for default:bf431b2a6ba6
```

The tracked workflow is `automation/n8n/workflows/github-pr-edge-sync.json`.
The repository-owned `automation/n8n/scripts/import-workflows.sh` command
creates the protected Header Auth credential, binds it to both the Webhook and
actuator nodes, publishes the managed workflow, restarts n8n, and runs a safe
unsupported-action canary against the production Webhook. There is no tracked
n8n Schedule Trigger and no direct GitHub webhook registration to n8n. No n8n
UI setup is part of the deployment path.

The authenticated router `/fallback` endpoint remains available for deliberate
operator recovery/full-registry intake. It is not called periodically.

## 3. Host prerequisites

Run host operations on the Ubuntu host, not inside the Hermes worker container.
The worker container has no host Docker socket, sudo, systemd, or host SSH
authority.

Install/start the control plane:

```bash
automation/n8n/scripts/host-install.sh --enable-docker-service
```

The n8n listener stays on loopback. `lease-controller` and `github-router` also
bind only to loopback and use host networking so they can reach the existing
Tailnet-only Hermes dashboard.

Do not mount the Docker socket into n8n/router services.

## 4. Least-privilege Hermes service authorization

Install the user plugin:

```bash
automation/n8n/scripts/configure-hermes-service-auth.sh \
  --hermes-home "$HOME/.hermes"
```

The installer fails closed unless allowlisted job ID `bf431b2a6ba6` exists
exactly once in profile `default`. It does not create or modify the job.

Restart the existing Hermes dashboard through its current supervisor so the
plugin registers the exact trigger/pause routes.

The plugin token can authorize only:

- `POST /api/cron/jobs/bf431b2a6ba6/trigger?profile=default`
- `POST /api/cron/jobs/bf431b2a6ba6/pause?profile=default`

It cannot list, create, edit, delete, or trigger another job.

## 4a. Completion-side edge wake

GitHub-backed worker completion uses the supported Hermes lifecycle observer
boundary rather than polling:

```text
core kanban_complete (commit provisional DONE)
  -> kanban_task_completed observer
  -> fixed live edge: kanban-github-sync.py --board <slug> --json
  -> existing edge projection (DONE -> REVIEW for an open/unmerged PR)
```

Install and activate the repository-owned observer manually in the Hermes
runtime namespace (the edge runtime deployment is a separate step):

```bash
automation/hermes/scripts/deploy-intake-edge.sh \
  --hermes-home "$HOME/.hermes"
automation/hermes/scripts/install-github-completion-edge-wake.sh \
  --hermes-home "$HOME/.hermes"
```

`install-github-completion-edge-wake.sh` validates a candidate copy before an
atomic plugin-directory switch, retains a timestamped backup, enables only the
named plugin, and prints rollback commands. Restart the existing Hermes worker
supervisor after activation; a plugin installed on disk but not loaded by the
worker is not runtime evidence.

The observer is not a completion/state owner. It reads the committed task row,
skips ordinary tasks, validates the authoritative board/runtime paths, and
uses one fixed `shell=False` command with bounded timeout/output. Invalid input
or a wake failure logs a bounded diagnostic and leaves provisional `DONE`; it
does not claim a merge or mutate Kanban. The existing edge remains the sole
`DONE`/`REVIEW` transition owner and optimistic updates make repeated
observations idempotent. No n8n workflow, Schedule Trigger, cron edit, or
public endpoint is added by this path.

## 5. Router credentials and public ingress

Copy the current GitHub credential and Hermes service token into the protected
router secret directory and create a stable webhook HMAC secret:

```bash
automation/n8n/scripts/configure-github-router-secrets.sh \
  --hermes-home "$HOME/.hermes"
```

Set the reviewed public HTTPS endpoint for the router in the runtime `.env`:

```dotenv
GITHUB_ROUTER_PUBLIC_URL=https://<reviewed-host>/github/hermes-intake
# Positive decimal installation.id for the configured GitHub App installation.
GITHUB_ROUTER_INSTALLATION_ID=<installation-id>
```

Expose only that reviewed HTTPS path through the reverse proxy/Funnel. Do not
publish ports `5678`, `5680`, or `5681` directly.

### Funnel path isolation (required)

The Tailscale Funnel terminates TLS for the funnel-enabled port (the deployed
host funnels `:10000`; the tailnet-only `:443` listener must stay
non-funnel). A funnel rule that proxies `/` publishes every path the backend
listens on to the public internet, so the funnel must proxy **only** the
router intake path. Configure the host as:

```bash
tailscale serve --https=10000 \
  --set-path=/github/hermes-intake http://127.0.0.1:5681/github/hermes-intake
```

Do not add a `/` (or any non-intake) funnel rule against the control-center
port (`8940`). The dashboard, `/voice`, `/avatar`, `/ramstation`, and the
other tailnet-only listeners on `:443`/`:8443`/`:9443` stay inside the
tailnet; they must not be publicly routed.

Verify after applying (from outside the tailnet, e.g. a non-Tailscale
network):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<ts-hostname>:10000/                  # expect 404 (not 200)
curl -s -o /dev/null -w '%{http_code}\n' https://<ts-hostname>:10000/healthz           # expect 200 {"ok":true} only
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://<ts-hostname>:10000/github/hermes-intake                                     # expect 401 invalid_signature
```

The external `/healthz` response is deliberately minimal (`{"ok": true}`);
queue depth and secret-configuration details moved to the Bearer-authenticated
`/debug/state` endpoint (see below).

### Router operator diagnostics

`GET /debug/state` returns the queue statistics and secret/URL configuration
flags that `/healthz` no longer exposes. It requires the intake control
token (the same Bearer token as `/scope/claim`; the router reads it from
`/run/secrets/hermes-intake-control-token`, managed by
`configure-github-router-secrets.sh`). Run from the host:

```bash
TOKEN_FILE="$HOME/.local/state/hermes-n8n-control-plane/secrets/hermes-intake-control-token"
curl -sS -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  http://127.0.0.1:5681/debug/state
```

- No/wrong token → `401 authorization_required`.
- Success → `200` with `queued_scopes`, `managed_count`,
  `public_url_configured`, `github_token_configured`,
  `webhook_secret_configured`, and `hermes_token_configured`.

Do not expose `/debug/state` through the funnel; the loopback-only binding of
the router port keeps it off the public internet.

Restart the control-plane services after configuration changes.

### Delivery replay deduplication

The router deduplicates redelivered GitHub webhooks by `X-GitHub-Delivery` ID
before any downstream wake. Two knobs control the bounded store:

```dotenv
GITHUB_ROUTER_DELIVERY_TTL_SECONDS=3600
GITHUB_ROUTER_DELIVERY_MAX_ENTRIES=4096
```

- Duplicate validly-signed deliveries inside the TTL answer `202` with
  `duplicate=true` and `reason=duplicate_delivery` and wake nothing.
- Missing or invalid `X-GitHub-Delivery` is rejected fail-closed (`400
  invalid_delivery_id`) before any dispatch; invalid or missing signatures are
  rejected before deduplication and never recorded.
- A delivery that fails dispatch (`502`) releases its record so the GitHub 5xx
  retry or an operator resend can dispatch again.
- The store persists in the router state file and survives restarts; entries
  expire by TTL and the map is capped at `GITHUB_ROUTER_DELIVERY_MAX_ENTRIES`
  with oldest-entry eviction, so it cannot grow unbounded.
- Operator canary events must use a fresh `X-GitHub-Delivery` UUID per test
  event; see `docs/GITHUB_EVENT_CONCURRENCY.md` for the full contract.

## 6. Webhook registry reconciliation

Repository membership is discovered from GitHub topic `hermes-agent`.
Reconcile whenever a repository is added/removed from that topic, credentials
change, or webhook state is repaired:

```bash
automation/n8n/scripts/reconcile-github-router.sh
```

The reconciliation operation creates/updates one router webhook for current
managed repositories and removes only router-owned matching webhooks from
repositories that are no longer managed. It does not change Kanban or Hermes
cron state.

There is intentionally no five-minute reconciliation scheduler in this scope.

## 7. Importing the on-demand edge-sync workflow

The repository tracks exactly one inactive edge-sync workflow:
`automation/n8n/workflows/github-pr-edge-sync.json`.
`render_workflows.py` generates that Webhook graph and never generates a
Schedule Trigger. `import-workflows.sh` clears only its own rendered
`github-*.json`/legacy `schedule-*.json` files, renders the current workflow,
and deploys it through the local n8n server CLI.

Run the complete host deployment command:

```bash
automation/n8n/scripts/import-workflows.sh
```

The command fails closed unless the protected token file exists with safe
permissions, the actuator health response contains
`edge_sync_runtime_ready=true`, and n8n exposes the required
`import:credentials`, `import:workflow`, `publish:workflow`, and
`export:workflow` server CLI commands. It imports one stable managed credential
and one stable managed workflow, so repeated runs update only those records and
do not delete unrelated workflows. n8n 2.x persists publication in the
database but requires an n8n restart before production workers use the new
published version; the command waits for `/healthz` after that restart.

The command generates the decrypted `httpHeaderAuth` import record with header
name `Authorization` and value `Bearer <control-token>`, binds its ID/name to
both the Webhook and `Run edge sync actuator` nodes, and keeps the credential
JSON plus canary curl config in a private `0600` temporary directory under the
external state root. The cleanup trap removes that directory on success or
failure; the token is never printed or placed in a tracked workflow, `.env`
example, GitHub payload, command argument, or log message.

If an older persisted n8n database still contains:

```text
Hermes schedule · GitHub agent-ready Issue intake
```

keep that workflow inactive or delete the persisted n8n workflow record. Do not
activate it. The old persisted record is separate from the new edge-sync
workflow and from Hermes job `bf431b2a6ba6`.

This persisted n8n cleanup is separate from the Hermes job. Never delete the
Hermes `bf431b2a6ba6` job while removing the old n8n Schedule workflow.

The command's production canary sends a bounded `pull_request` payload with an
unsupported `opened` action and requires the workflow's explicit
`unsupported_pull_request_action` no-op response. It must not resolve a board
or invoke the actuator side effect. The same protected token is sent by
`github-router` to the n8n Webhook and by n8n to the actuator. Do not put the
token in tracked workflow JSON, `.env.example`, GitHub payloads, or logs.

This canary is only an authenticated production Webhook/import evidence gate.
The full host-runtime gate still requires one validly signed GitHub delivery or
redelivery, with router HMAC/delivery/repository admission evidence and the
expected downstream actuator result. A repository test or the unsupported
canary does not claim that live signed-delivery gate passed.

## 8. One-time transition from active polling to async-only

If the live Hermes intake job is already paused and signed GitHub events are
successfully waking it, leave it paused; no further schedule mutation is
required.

If a host is still running the legacy Hermes five-minute schedule, first prove
the event path without deleting or editing the job:

1. verify `github-router` and `lease-controller` are healthy;
2. reconcile the intended `hermes-agent` repositories;
3. deliver a signed GitHub test event;
4. confirm the event causes one fresh successful run of `bf431b2a6ba6`;
5. confirm the latest lease returns the same job to paused state;
6. only then disable recurring execution by pausing that existing job.

The repository's historical `cutover.sh` remains a snapshot/rollback utility
for the one-job boundary. Its confirmation flag name predates the async-only
router and should not be interpreted as permission to reactivate an n8n
Schedule workflow. Do not run cutover again on a host that is already in the
intended paused-between-events state.

## 9. Runtime canary

A successful event canary must establish all of these facts:

- router accepted a valid `X-Hub-Signature-256` event for a managed repository;
- the canary event carries a **fresh** `X-GitHub-Delivery` UUID; within the
  dedupe TTL the router rejects the same delivery ID as a `202` duplicate
  no-op (contract behavior, not a failure);
- exactly one repository scope was queued;
- lease-controller returned a lease and called the preserved Hermes job;
- Hermes `last_run_at` advanced and `last_status=ok` for `bf431b2a6ba6`;
- after bounded cleanup, the same job is `enabled=false` / `state=paused`;
- no n8n Schedule Trigger fired the intake.
- the active n8n workflow has one Webhook execution for the canary and one
  actuator call for a supported PR event; unsupported PR actions are no-ops.

A build/static check is not runtime evidence.

## 10. Recovery and deliberate full intake

The router keeps `/fallback` as an authenticated operator recovery endpoint.
It performs webhook reconciliation, enqueues a full-registry scope, and wakes
the same preserved Hermes job. No schedule calls this endpoint automatically.

Prefer the normal signed GitHub event path. Use `/fallback` only when a full
re-scan is intentionally required.

If router/lease state is unhealthy, do not recreate the Hermes job. Repair the
control plane, verify the allowlisted job still exists uniquely, and retry the
canary.

## 11. Legacy rollback snapshots

`automation/n8n/scripts/cutover.sh rollback` remains bounded to
`default:bf431b2a6ba6` and rejects snapshots that contain other jobs or belong
to another Hermes home. Existing tests enforce that boundary.

Before using a legacy rollback snapshot, ensure old n8n Schedule workflows are
inactive so rollback cannot produce dual scheduling.

Do not hand-edit snapshot targets to bypass the one-job boundary.

## 12. Validation

Local deterministic validation:

```bash
python3 automation/n8n/scripts/validate.py
python3 tests/test_n8n_import_contract.py
python3 tests/test_github_event_concurrency_contract.py
python3 tests/test_github_router.py
python3 tests/test_github_intake_actuator.py
python3 tests/test_intake_completion_contract_entrypoint.py
PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 \
  tests/test_completion_edge_wake_plugin.py
python3 tests/test_intake_lease_controller.py
/ws/hermes-agent/venv/bin/python3 tests/test_n8n_cron_auth_plugin.py
/ws/hermes-agent/venv/bin/python3 tests/test_hermes_cron_trigger_pause.py
python3 tests/test_repo_scoped_intake.py
python3 tests/test_repository_registry.py
python3 tests/test_repository_registry_board_workdir.py
python3 tests/test_cutover_snapshot_boundary.py
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-rework.py
```

Live-host evidence should additionally retain:

- `docker compose ps` showing expected services healthy;
- `curl`/health evidence for loopback router and lease-controller;
- webhook reconciliation result;
- one signed event execution record;
- Hermes job `last_run_at`, `last_status`, and final paused state;
- confirmation that no n8n intake Schedule workflow is active.

GitHub-hosted Actions are intentionally not the required validation surface;
follow `AGENTS.md` for local validation rules.
