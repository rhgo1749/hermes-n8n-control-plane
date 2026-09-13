# GitHub event intake concurrency guard

## Final event topology

Repository-specific n8n GitHub Trigger workflows and the n8n Schedule fallback
are retired. Production intake is event-driven through one loopback GitHub
router. Issue/intake events use the lease-controller plus fixed direct intake
actuator; PR completion/rework events use one private n8n Webhook hop and the
existing edge reconciliation script. A router-local low-frequency safety tick
also reuses the same durable full-intake scope path so a single missed webhook
cannot strand an `agent-ready` Issue indefinitely.

```text
GitHub App webhook (or reconciled repository webhook)
  -> HTTPS ingress
  -> github-router :5681
       -> HMAC verification
       -> X-GitHub-Delivery replay dedupe
       -> owner/install admission and bounded repository extraction
       -> pull_request event for a managed repository: private n8n Webhook :5678
            -> allowlisted filter/normalization
            -> fixed actuator :5682
                 -> kanban-github-sync.py --board <slug> --json
       -> new/unknown or other intake event: durable FIFO wake-scope queue
            -> lease-controller :5680
                 -> fixed intake actuator :5682
                      -> deployed github-agent-ready-kanban-intake.py
       -> hourly safety tick: durable full-intake scope
            -> same lease-controller and same direct actuator
```

The App webhook is the discovery boundary for repositories that are not yet in
the read-only registry. `installation`, `installation_repositories`,
`repository` (the documented `archived` action), `public`, `issues`, `issue_comment`, `pull_request`, and
`pull_request_review` deliveries are accepted only for the configured owner
scope. An installation delivery may contain up to 100 repositories; duplicate
names are folded case-insensitively before one FIFO scope is queued. A valid
owner installation delivery for an unknown repository is queued rather than
rejected as `repository_not_managed`, allowing the intake worker to perform the
fresh metadata/topic/contract checks and checkout provisioning asynchronously.
Installation deliveries without repository candidates are an acknowledged
no-op. Foreign owners, invalid repository identities, oversized batches, and
missing repository objects fail closed without queueing.

The router never installs or changes a GitHub App, webhook, token, or
permission. The operator must configure the App's signed webhook and HTTPS
ingress separately. `GITHUB_ROUTER_OWNER_TYPE` is explicitly `personal` or
`organization`; organization mode is never inferred from the owner name.
`GITHUB_ROUTER_INSTALLATION_ID` is a required positive decimal configuration
value matching the installed App's `installation.id`. Every payload containing
`installation` must carry that same numeric identity; an optional nested
`installation.account` is checked for owner/type strengthening when present,
but is not required for repository-bearing events.

The direct actuator is the single intake execution primitive. The legacy Hermes
intake cron job was retired after the direct-actuator cutover and successful
live canary; its absence is valid on current hosts. An accepted lease invokes
the fixed actuator, and delayed cleanup closes only the lease locally rather
than pausing a persistent schedule.

The router entrypoint runs a bounded full-intake safety wake with a default
interval of 3600 seconds and rejects configured intervals below 300 seconds.
The first safety wake occurs only after one full interval; router startup does
not immediately scan. Each tick calls the existing `_enqueue_scope(full=True)`
and `_wake()` boundaries only. A stable synthetic scope identity prevents a
queued, in-flight, or durable-pending safety scope from being duplicated. A
queued scope may be re-woken after an earlier wake failure; in-flight or pending
work is not double-woken. The safety tick does not call webhook reconciliation,
does not inspect `agent-*` labels itself, and introduces no new state store or
lifecycle owner. Normal signed webhook delivery remains the primary intake
path.

Each non-PR intake event, including a first App delivery for an unknown
repository, enqueues its repository scope before waking the direct actuator.
Each intake invocation claims exactly one queued scope. Expired unclaimed scopes are
recovered with a bounded attempt/backoff or retained in the durable pending list
when the retry limit is reached; they are never silently dropped. Claimed scopes
carry a restart-safe lease and a fencing token. A worker must acknowledge with
that token only after onboarding, board bootstrap, and task work complete; a
stale worker cannot acknowledge a scope reclaimed by a later worker. Retryable
API/clone/lock/registry/board failures are requeued, while permanent repository
validation skips are acknowledged with structured skip evidence. A PR event is
sent only as bounded normalized data to the private n8n Webhook; n8n never
receives the external signature boundary or a caller-controlled command. The
persisted lease-controller remains the stale delayed-cleanup correctness guard
for the direct-actuator path; n8n's `N8N_CONCURRENCY_PRODUCTION_LIMIT=1`
remains only a load limiter.

The router exposes the worker control contract only through authenticated POST
requests: `/scope/claim` returns one scope plus `claim_token`, `/scope/ack`
releases that exact claim, and `/scope/requeue` records the bounded retry reason.
The acknowledgment and requeue endpoints are not safe GET operations; a stale or
mismatched token is rejected without changing queue state.


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
Its first invocation is bounded by the shared edge timeout contract. If that
attempt times out, the observer re-reads the committed task row before deciding
whether another edge process is necessary. If the earlier owner already moved
the task away from provisional `DONE`, the observer returns without a duplicate
edge run. If the GitHub-backed task is still `DONE`, or that re-read cannot be
trusted, the observer launches exactly one new fixed-argv edge child with a
completely fresh deadline. There is no sleep loop, Schedule Trigger, or polling
fallback in this completion-observer retry path. Non-timeout failures are not
retried. Only when that fresh retry also times out is the completion wake
reported as a final `edge_retry_timeout` failure.

The process-level regression deliberately starts an owner before the simulated
completion is committed, so the owner's snapshot cannot contain that completion.
The first completion child is forced to expire in lock contention; the test
passes only when a post-owner retry actually enters the canonical edge and sees
the later committed snapshot. A focused unit contract separately verifies that
a task already projected away from `DONE` suppresses the retry. A timeout
diagnostic by itself is therefore not success evidence.

This serializes the complete edge run, including GitHub reads and Kanban/GitHub
side effects, without introducing a queue database, task store, or second
transition owner.

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

Reconciliation remains an explicit maintenance action rather than a polling
side effect:

```bash
automation/n8n/scripts/reconcile-github-router.sh
```

The authenticated `/fallback` endpoint remains available for deliberate
operator recovery/full-registry intake. Separately, the router entrypoint's
low-frequency safety tick enqueues the same canonical full-intake scope without
calling `/reconcile`; no tracked n8n workflow or n8n Schedule Trigger calls
`/fallback` automatically. The tracked workflow is
`automation/n8n/workflows/github-pr-edge-sync.json`; the repository-owned
`automation/n8n/scripts/import-workflows.sh` binds its loopback Header Auth
credential, publishes it, and runs an unsupported-action production canary.
A live signed GitHub delivery or redelivery remains the host-runtime evidence
gate for the primary event path; a live safety-wake read-back is the additional
evidence gate for missed-webhook self-heal.
