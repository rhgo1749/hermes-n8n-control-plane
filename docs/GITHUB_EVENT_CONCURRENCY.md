# GitHub event intake concurrency guard

## Final event topology

Repository-specific n8n GitHub Trigger workflows and the n8n Schedule fallback
are retired. Production intake is event-driven through one loopback GitHub
router. Issue/intake events continue to use the existing Hermes cron primitive;
PR completion/rework events use one private n8n Webhook hop and the existing
edge reconciliation script.

```text
GitHub repository webhook
  -> HTTPS ingress
  -> github-router :5681
       -> HMAC verification
       -> X-GitHub-Delivery replay dedupe
       -> managed repository admission
       -> pull_request event: private n8n Webhook :5678
            -> allowlisted filter/normalization
            -> fixed actuator :5682
                 -> kanban-github-sync.py --board <slug> --json
       -> other intake event: durable FIFO wake-scope queue
            -> lease-controller :5680
                 -> existing Hermes job default:bf431b2a6ba6 trigger
```

The Hermes job itself is preserved. Its stored job ID, name, script, schedule,
and ownership are not migrated into n8n. Between event-driven invocations the
job normally remains paused; an accepted lease temporarily triggers that same
job and the latest lease alone may pause it again.

Each non-PR intake event enqueues its repository scope before triggering Hermes.
Each intake invocation claims exactly one queued scope. Expired unclaimed
scopes are pruned. A PR event is sent only as bounded normalized data to the
private n8n Webhook; n8n never receives the external signature boundary or a
caller-controlled command. The persisted lease-controller remains the stale
delayed-pause correctness guard for the Hermes path; n8n's
`N8N_CONCURRENCY_PRODUCTION_LIMIT=1` remains only a load limiter.

The n8n edge workflow accepts only:

- `pull_request` + `action=closed` + `merged=true`;
- `pull_request` + `action=labeled` + `label=agent-rework`.

All other PR actions finish as an explicit no-op. The actuator repeats the
allowlist, resolves repository → board through the existing registry/task
provenance, and runs exactly `kanban-github-sync.py --board <slug> --json`.

## Edge reconciliation single-flight

Every invocation of the deployed edge path, regardless of whether it came from
the webhook actuator or the completion observer, enters the same process-shared
boundary in the canonical edge implementation before any GitHub/Kanban
reconciliation read or side effect:

- Linux `fcntl.flock(LOCK_EX)` guards
  `$HERMES_HOME/kanban/.resource-locks/github-edge-sync.lock`;
- the runtime root, lock directory, and lock file are validated fail-closed
  against symlink/path substitution, and the lock is outside tracked
  repository state;
- the actuator's process-local `_RUN_LOCK` remains a fast admission guard, but
  the filesystem lock is the correctness boundary shared with direct plugin
  wakes;
- acquisition blocks in the kernel rather than polling or sleeping, and a
  crashed owner releases the kernel lock.

The completion observer must not consume a completion signal merely because
its first child used the whole outer deadline waiting behind an earlier owner.
Its first invocation is bounded by the shared edge timeout contract; if that
attempt times out, the observer immediately launches exactly one new fixed-argv
edge child with a completely fresh deadline. There is no sleep loop, Schedule
Trigger, or polling fallback. Non-timeout failures are not retried. Only when
that fresh retry also times out is the completion wake reported as a final
`edge_retry_timeout` failure.

The process-level regression deliberately starts an owner before the simulated
completion is committed, so the owner's snapshot cannot contain that completion.
The first completion child is forced to expire in lock contention; the test
passes only when a post-owner retry actually enters the canonical edge and sees
the later committed snapshot. A timeout diagnostic by itself is therefore not
success evidence.

This serializes the complete edge run, including GitHub reads and Kanban/GitHub
side effects, without introducing a queue database, task store, second
transition owner, or polling fallback.

## Delivery replay deduplication

GitHub may redeliver the same webhook delivery (GitHub-side retry, operator
resend, network retransmit). A validly signed replay must not trigger the
Hermes job twice, so the router verifies the `X-GitHub-Delivery` header on
every signed intake event:

- Missing or invalid `X-GitHub-Delivery` is rejected fail-closed with `400
  invalid_delivery_id` before any downstream dispatch. Valid delivery IDs are
  trimmed and must match `[A-Za-z0-9._:-]{1,128}`.
- A first-time delivery inside its TTL is recorded in the persistent router
  state file (`delivery_dedupe` map, one `created_at`/`expires_at` entry per
  delivery ID) before the event is processed, then enqueued and dispatched as
  usual.
- A redelivered (duplicate) delivery ID inside its TTL is a `202` no-op with
  `duplicate=true` and `reason=duplicate_delivery`; it enqueues no scope and
  calls no `_wake()`, and its TTL is not refreshed.
- Different delivery IDs are independent events even when the payload body is
  identical.
- Invalid or missing signatures are rejected before deduplication and never
  recorded, so a forged replay cannot poison the store and a later valid
  delivery of the same ID still processes.
- A delivery that fails dispatch (HTTP 502 from the lease controller) has its
  record released so the GitHub 5xx retry or an operator resend can dispatch
  again.
- The store is bounded on both dimensions: entries expire after
  `GITHUB_ROUTER_DELIVERY_TTL_SECONDS` (default `3600`) and the map is capped
  at `GITHUB_ROUTER_DELIVERY_MAX_ENTRIES` (default `4096`) with oldest-entry
  eviction. It persists in the router state file, so restarts keep deduplicating
  within the TTL window.
- The dedupe store is the only new state in the router state file; the scope
  queue, managed-repository registry, stale-pause lease, and reconciliation
  behavior are unchanged. Operator-sent canary events must therefore use a
  fresh `X-GitHub-Delivery` UUID each time; reusing one within the TTL is a
  valid duplicate no-op by contract.

## Registry reconciliation and fallback

Webhook reconciliation is topic-driven. `github-router /reconcile` discovers
repositories carrying `hermes-agent`, creates or updates the router webhook,
and removes only matching router-owned webhooks for repositories that leave the
topic. It never deletes boards, tasks, or Hermes cron jobs.

Reconciliation is now an explicit maintenance action rather than a five-minute
polling side effect:

```bash
automation/n8n/scripts/reconcile-github-router.sh
```

The authenticated `/fallback` endpoint remains available for deliberate
operator recovery/full-registry intake, but no tracked n8n workflow calls it
automatically. The tracked workflow is
`automation/n8n/workflows/github-pr-edge-sync.json`; it is inactive until its
loopback Header Auth credentials are bound and a host canary passes.