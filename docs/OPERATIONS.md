# Operations runbook — async-only GitHub intake

## Issue #87 onboarding diagnostics

The first supported GitHub App delivery is acknowledged by the router and
placed in the existing durable scope queue. The existing intake authority then
revalidates the owner, archive state, `hermes-agent` topic, default branch, and
contract visibility before reading registry board intent or touching
`/ws/projects/<repository-name.casefold()>`.
It never relies on a manual webhook reconciliation for first discovery.

Run the read-only diagnostic in the namespace that owns the active Hermes
runtime. For the current containerized deployment, invoke it from the Ubuntu
host through `hermes-cloudcli-agent`:

```bash
docker exec hermes-cloudcli-agent bash -lc '
  cd /ws/projects/hermes-n8n-control-plane &&
  automation/n8n/scripts/diagnose-github-onboarding.sh \
    --hermes-home /home/hermes/.hermes
'
```

Do not substitute the Ubuntu user's `$HOME/.hermes` unless that path is itself
the active Hermes runtime. In the current deployment it is not; the container
runtime owns `/home/hermes/.hermes`.

The command verifies that the deployed intake wrapper/core expose all required
onboarding entrypoints and that the current intake execution contract is the
fixed loopback direct actuator (`:5682`) with `hermes_cron_required=false`. It
also checks the loopback router, lease-controller, intake-actuator readiness,
and the lease-controller `POST /trigger` surface without triggering intake.
`--skip-network` checks only the deployed script/runtime boundary.

Interpret bounded failures as follows: `invalid_signature` or
`authorization_required` means the protected ingress boundary rejected the
request; `repository_not_opted_in`, `repository_archived`,
`owner_scope_mismatch`, or `contract_visibility_invalid` means the fresh
GitHub metadata gate rejected the repository; `checkout_path_conflict`,
`checkout_origin_mismatch`, `checkout_dirty`,
`checkout_default_branch_invalid`, `checkout_default_branch_mismatch`,
`checkout_diverged`, `checkout_materialization_unsafe`,
`checkout_ancestry_failed`, `checkout_unshallow_failed`,
`checkout_fetch_failed`, `checkout_fast_forward_failed`,
`repository_lock_busy`, or `clone_failed` means no existing checkout was
overwritten and only the current attempt's temporary path is eligible for
cleanup. A newly registered
checkout is intentionally retained when a later registry or board step fails;
rerun the operator recovery after fixing the reported boundary. A missing or
invalid deployed intake wrapper/core, unavailable direct actuator, or unhealthy
loopback control-plane service is an operator stop. Do not recreate the retired
legacy Hermes intake cron job as a recovery action.

GitHub App installation, permissions, public HTTPS/Funnel routing, protected
secret provisioning, host `/ws/projects` ownership, production canary, and
post-merge edge reconciliation are host-owned validation gates. They are not
proven by repository-local tests.

## 1. Direct intake runtime ownership

GitHub agent-ready intake executes through the deployed Hermes-side wrapper/core
via the fixed loopback intake actuator on `127.0.0.1:5682`. The
lease-controller owns wake serialization and lease state; it does not trigger or
pause a Hermes cron job.

The legacy intake cron job `default:bf431b2a6ba6` was retired after the direct
actuator cutover and live canary. Its absence is valid current-state evidence,
not a repair condition. Do not recreate it. Historical cron snapshots and output
may remain for rollback archaeology, but they are not part of the active intake
path.

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
                 -> fixed intake actuator 127.0.0.1:5682
                      -> deployed github-agent-ready-kanban-intake.py
       -> hourly safety tick -> durable full-intake scope
            -> same lease-controller -> same direct actuator
```

The tracked workflow is `automation/n8n/workflows/github-pr-edge-sync.json`.
The repository-owned `automation/n8n/scripts/import-workflows.sh` command
creates the protected Header Auth credential, binds it to both the Webhook and
actuator nodes, publishes the managed workflow, restarts n8n, and runs a safe
unsupported-action canary against the production Webhook. There is no tracked
n8n Schedule Trigger and no direct GitHub webhook registration to n8n. No n8n
UI setup is part of the deployment path.

The authenticated router `/fallback` endpoint remains available for deliberate
operator recovery/full-registry intake. It is not called periodically. The
router-local safety tick is separate: after one full configured interval it
enqueues the same canonical durable full-intake scope directly, without calling
`/fallback` or webhook reconciliation. Signed GitHub delivery remains the
primary intake path; the safety tick is only a missed-webhook liveness backstop.

## 3. Host prerequisites

Run **control-plane host operations** on the Ubuntu host, not inside the Hermes
worker container. The worker container has no host Docker socket, sudo, systemd,
or host SSH authority. Conversely, operations that install or validate Hermes
runtime scripts/plugins must run in the namespace that owns the active Hermes
runtime; in the current deployment that is `hermes-cloudcli-agent` with
`/home/hermes/.hermes`. Do not treat the Ubuntu user's unrelated `$HOME/.hermes`
as the live runtime.

Install/start the control plane:

```bash
automation/n8n/scripts/host-install.sh --enable-docker-service
```

The n8n listener stays on loopback. `lease-controller` and `github-router` also
bind only to loopback and use host networking so they can reach the existing
Tailnet-only Hermes dashboard.

Do not mount the Docker socket into n8n/router services.

## 4. Least-privilege direct intake actuator

Install or refresh the fixed Hermes-side actuator from the Ubuntu host:

```bash
automation/n8n/scripts/install-intake-actuator.sh
```

The actuator listens only on loopback `127.0.0.1:5682`, accepts the protected
intake-control credential, and exposes only the fixed intake/edge operations
implemented by the reviewed runtime. It does not accept caller-controlled shell
commands, script paths, profiles, or arbitrary Hermes routes. The
lease-controller calls this actuator directly; no Hermes dashboard cron-auth
plugin or legacy intake cron job is required.

## 4a. Completion-side edge wake

GitHub-backed worker completion uses the supported Hermes lifecycle observer
boundary rather than polling:

```text
core kanban_complete (commit provisional DONE)
  -> kanban_task_completed primary observer
  -> fixed live edge: kanban-github-sync.py --board <slug> --json
  -> existing edge projection (DONE -> REVIEW for an open/unmerged PR)

if that primary observer hook is silently missed:
existing dispatcher tick
  -> github-completion-dispatch-safety-wake
  -> replay primary completion observer for one eligible still-DONE task
  -> same fixed live edge / same edge lock
```

Install and activate the repository-owned completion observers manually in the
Hermes runtime namespace (the edge runtime deployment is a separate first
step). For the current containerized deployment, run from the Ubuntu host:

```bash
docker exec hermes-cloudcli-agent bash -lc '
  cd /ws/projects/hermes-n8n-control-plane &&
  automation/hermes/scripts/deploy-intake-edge.sh \
    --hermes-home /home/hermes/.hermes &&
  automation/hermes/scripts/install-github-completion-edge-wake.sh \
    --hermes-home /home/hermes/.hermes \
    --hermes-bin /home/hermes/.local/bin/hermes &&
  automation/hermes/scripts/install-github-completion-dispatch-safety-wake.sh \
    --hermes-home /home/hermes/.hermes \
    --hermes-bin /home/hermes/.local/bin/hermes
'
```

`install-github-completion-edge-wake.sh` validates a candidate copy before an
atomic plugin-directory switch, retains a timestamped backup, enables only the
named primary plugin, and prints rollback commands. The safety-wake installer
installs/enables only its bounded companion plugin; it requires the primary
completion plugin and deployed edge runtime to be present.

Restart the existing Hermes worker process after primary-observer activation,
and restart the long-lived Hermes gateway/dispatcher after safety-wake
activation so the dispatcher hook is registered. A plugin installed on disk
but not loaded by its owning long-lived process is not runtime evidence.

Neither observer is a completion/state owner. The primary observer reads the
committed task row, skips ordinary tasks, validates the authoritative
board/runtime paths, and uses one fixed `shell=False` command with bounded
timeout/output. The safety wake reads only recent eligible completion evidence
on the current board and replays the primary observer; it does not write Kanban
state or poll GitHub. Invalid input or a wake failure leaves provisional
`DONE`; it does not claim a merge. The existing edge remains the sole
`DONE`/`REVIEW` transition owner and optimistic updates plus the shared edge
single-flight lock make repeated observations idempotent. No n8n Schedule
Trigger, cron edit, second state store, or public endpoint is added by either
completion-wake path.

## 5. Router credentials and public ingress

Copy the current GitHub credential and Hermes service token into the protected
router secret directory and create a stable webhook HMAC secret:

```bash
automation/n8n/scripts/configure-github-router-secrets.sh
```

This is a host control-plane operation. The script resolves the protected
external n8n state root and does not need a Hermes home. A hidden
`--hermes-home` compatibility argument is accepted only for older operator
notes and is intentionally ignored; new procedures must not use it.

Set the reviewed public HTTPS endpoint for the router in the runtime `.env`:

```dotenv
GITHUB_ROUTER_PUBLIC_URL=https://<reviewed-host>/github/hermes-intake
# Optional for existing managed repository webhooks. Required for GitHub App
# first-discovery/onboarding; use the positive decimal installation.id when enabled.
GITHUB_ROUTER_INSTALLATION_ID=<installation-id>
```

The control-plane owns only the reviewed `/github/hermes-intake` public route.
Do not publish ports `5678`, `5680`, or `5681` directly. If the same Funnel port
also hosts another deliberately reviewed service, preserve that unrelated route
instead of treating the whole port as control-plane-owned.

### Funnel path isolation (required)

The Tailscale Funnel terminates TLS for the funnel-enabled port (the deployed
host funnels `:10000`; the tailnet-only `:443` listener must stay
non-funnel). Reassert only the control-plane intake path; do not remove or
overwrite unrelated reviewed routes that intentionally share the same Funnel
port. Configure the intake path as:

```bash
tailscale funnel --bg --yes --https=10000 \
  --set-path=/github/hermes-intake \
  http://127.0.0.1:5681/github/hermes-intake
```

Do not add a `/` (or any non-intake) funnel rule against the control-center
port (`8940`). The dashboard, `/voice`, `/avatar`, `/ramstation`, and the
other tailnet-only listeners on `:443`/`:8443`/`:9443` stay inside the
tailnet; they must not be publicly routed.

Verify after applying:

```bash
tailscale funnel status
# Confirm /github/hermes-intake maps to 127.0.0.1:5681 without changing
# unrelated reviewed routes on the same Funnel port.

curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://<ts-hostname>:10000/github/hermes-intake  # expect 401 invalid_signature
```

Router `/healthz` is a loopback diagnostic unless the operator deliberately
publishes a separate health route. Queue depth and secret-configuration details
remain on the Bearer-authenticated `/debug/state` endpoint (see below).

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
The router-local intake safety wake does not call webhook reconciliation.

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
activate it. The old persisted schedule is separate from the current Webhook
edge-sync workflow and from the direct-actuator intake path. The retired Hermes
intake cron job must not be recreated while cleaning up this historical n8n
record.

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

## 8. Legacy cron cutover status

The production intake path has already completed the one-time migration from the
Hermes five-minute cron primitive to the direct actuator. A current host may
therefore have no `default:bf431b2a6ba6` entry at all. That absence is expected
once the direct-actuator canary has passed.

For an older host that still has the legacy job, do not delete or mutate it
until the direct actuator, signed ingress, repository-scoped intake, and lease
cleanup have all passed. After that migration the job may be retired; do not
recreate it on already-migrated hosts.

The repository's historical `cutover.sh` is a legacy snapshot/rollback utility
for the former one-job boundary. Do not run it as a normal recovery mechanism
on a direct-actuator host.

## 9. Runtime canary

A successful event canary must establish all of these facts:

- router accepted a valid `X-Hub-Signature-256` event for a managed repository;
- the canary event carries a **fresh** `X-GitHub-Delivery` UUID; within the
  dedupe TTL the router rejects the same delivery ID as a `202` duplicate
  no-op (contract behavior, not a failure);
- exactly one repository scope was queued;
- lease-controller returned a lease and called the fixed direct actuator;
- the actuator accepted the fixed intake operation and returned to `busy=false`;
- the claimed scope was acknowledged (or deterministically requeued/pended on a bounded failure);
- repository/idempotency read-back proves the existing task identity was reused rather than duplicated;
- the lease converged to `active` or the bounded cleanup state `paused`;
- no Hermes or n8n Schedule Trigger fired the intake;
- the active n8n workflow has one Webhook execution for the canary and one
  actuator call for a supported PR event; unsupported PR actions are no-ops.

A build/static check is not runtime evidence. The router-local hourly safety
wake is an additional liveness path, not a substitute for this signed-delivery
primary-path canary.

## 10. Recovery and deliberate full intake

The router keeps `/fallback` as an authenticated operator recovery endpoint.
It performs webhook reconciliation, enqueues a full-registry scope, and wakes
the same lease-controller/direct-actuator intake path. No schedule calls this
endpoint automatically.

Prefer the normal signed GitHub event path. Use `/fallback` only when a full
re-scan plus reconciliation is intentionally required. The router-local safety
wake may independently enqueue the same canonical full-intake scope after its
configured interval, but it does not call `/fallback` or `/reconcile` and does
not create another intake implementation.

If router/lease/actuator state is unhealthy, repair the control plane and retry
the canary. Do not recreate the retired Hermes intake cron job.

## 11. Legacy rollback snapshots

Historical `cutover.sh` snapshots may still contain the retired
`default:bf431b2a6ba6` definition. They are retained only for archaeology and
migration rollback analysis. Restoring one onto a current direct-actuator host
would reintroduce a second intake execution primitive and is not an approved
normal recovery path.

Do not hand-edit or restore legacy snapshot targets to bypass the current
single direct-actuator ownership boundary.

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
PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 \
  tests/test_completion_dispatch_safety_wake.py
python3 tests/test_intake_lease_controller.py
python3 -m pytest -q tests/test_onboarding_diagnostics.py
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
- `curl`/health evidence for loopback router, lease-controller, and direct actuator;
- webhook reconciliation result;
- one signed event execution record;
- direct-actuator source/live identity plus final `busy=false` evidence;
- scope acknowledgment/idempotent task read-back and final lease state;
- confirmation that no legacy Hermes/n8n intake Schedule path is active.

GitHub-hosted Actions are intentionally not the required validation surface;
follow `AGENTS.md` for local validation rules.