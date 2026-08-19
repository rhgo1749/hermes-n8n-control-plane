# Operations runbook — async-only GitHub intake

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
       -> durable FIFO scope queue
       -> lease-controller 127.0.0.1:5680
            -> POST existing Hermes trigger route for default:bf431b2a6ba6
            -> existing Hermes ticker executes the existing intake script
            -> latest lease performs the bounded pause cleanup
```

There is no tracked n8n Schedule Trigger and no tracked per-repository n8n
GitHub Trigger workflow. n8n remains a private persistent service, but GitHub
intake execution does not depend on an n8n polling workflow.

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
```

Expose only that reviewed HTTPS path through the reverse proxy/Funnel. Do not
publish ports `5678`, `5680`, or `5681` directly.

Restart the control-plane services after configuration changes.

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

## 7. Retiring the old n8n polling workflow

The repository no longer tracks
`automation/n8n/workflows/schedule-github-agent-ready-intake.json`.
`render_workflows.py` reports an empty tracked workflow set, and
`import-workflows.sh` is a compatibility/status command that never imports a
replacement Schedule Trigger.

If an older persisted n8n database still contains:

```text
Hermes schedule · GitHub agent-ready Issue intake
```

keep that workflow inactive or delete the persisted n8n workflow record. Do not
activate it.

This persisted n8n cleanup is separate from the Hermes job. Never delete the
Hermes `bf431b2a6ba6` job while removing the old n8n Schedule workflow.

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
- exactly one repository scope was queued;
- lease-controller returned a lease and called the preserved Hermes job;
- Hermes `last_run_at` advanced and `last_status=ok` for `bf431b2a6ba6`;
- after bounded cleanup, the same job is `enabled=false` / `state=paused`;
- no n8n Schedule Trigger fired the intake.

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
python3 tests/test_github_event_concurrency_contract.py
python3 tests/test_github_router.py
python3 tests/test_intake_lease_controller.py
/ws/hermes-agent/venv/bin/python3 tests/test_n8n_cron_auth_plugin.py
python3 tests/test_hermes_cron_trigger_pause.py
python3 tests/test_repo_scoped_intake.py
python3 tests/test_repository_registry.py
python3 tests/test_repository_registry_board_workdir.py
python3 tests/test_cutover_snapshot_boundary.py
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
