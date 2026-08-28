# Edge worker resource admission

This control-plane extension protects the **edge-owned existing-PR rework
spawn lane** when several Hermes profiles share an execution resource with a
smaller concurrency capacity than the normal provider path.

It is intentionally not model-specific and does not modify Hermes core.
`edge/kanban-github-sync.py` remains the canonical GitHub/Kanban lifecycle
implementation.  The deployed entrypoint adds admission immediately before
that implementation's `_dispatch_pending_rework()` call.

## Why this exists

The normal edge rework lane already respects each board's
`kanban.max_in_progress` and the core board dispatch lock.  Those guards are
board-scoped.  Two different boards can therefore each be below their own cap
while both profiles ultimately use the same serial local inference endpoint.

A second failure mode exists when a task run has already recorded a terminal
outcome (for example `review_requested`) but the detached worker process has
not actually exited.  Rework can make the card READY again while that old PID
is still alive.  Starting the replacement worker at that point produces two
worker processes for one task even though only the newer run owns
`current_run_id`.

The resource-admission overlay treats a live worker process as the scarce
resource, rather than trusting task status alone.

## Configuration

The feature is opt-in under the existing `kanban:` section:

```yaml
kanban:
  # Existing board-local cap; unchanged.
  max_in_progress: 4

  worker_resources:
    local-inference:
      capacity: 1
      assignees:
        - "local-*"
        - "kanban-main-local"
      stale_worker_grace_seconds: 5
```

`local-inference` is an arbitrary operator-defined name.  It is **not** a
provider or model identifier and the code has no Qwen/llama.cpp/SGLang/model
name checks.

`assignees` accepts shell-style glob patterns (`fnmatch`).  Profiles that do
not match any resource group bypass the overlay completely and retain the
existing parallel dispatch behavior.  This is the backwards-compatibility
contract.

Examples:

```yaml
kanban:
  worker_resources:
    local-serial-llm:
      capacity: 1
      assignees: ["local-*"]

    shared-two-slot-endpoint:
      capacity: 2
      assignees: ["batch-a", "batch-b"]
```

A profile must match at most one group.  Ambiguous matches fail closed with
`resource_config_invalid`; group selection never depends on YAML ordering.

`capacity` must be a positive integer.  `stale_worker_grace_seconds` defaults
to 5 seconds and may be set from 0 through 60 seconds.

## Admission semantics

For a rework-pending READY task whose assignee matches a resource group:

1. Acquire a short host-local file lock for that resource group.  Lock files
   live under the board root's `.resource-locks/` directory and are derived
   from the core-resolved Kanban DB path, not from a hardcoded Hermes home.
2. Inspect the newest prior run of the same task.
   - Active run (`ended_at IS NULL`) + live PID: return `task_worker_active`;
     do not kill it and do not claim a new run.
   - Terminal run + dead PID: continue.
   - Terminal run + live PID whose Linux cmdline proves it is the Hermes
     worker for this exact task: SIGTERM the process group, wait the configured
     grace period, then SIGKILL only if necessary.  A replacement run is not
     claimed until that PID is gone.
   - Terminal run + PID that demonstrably belongs to another process: treat it
     as OS PID reuse; never signal it.
   - Live PID whose identity cannot be verified: fail closed.
3. Scan sibling Kanban board DBs for occupied slots in the same resource.
   Durable `task_runs.profile` / task assignee data determines membership.
   Live PIDs count even when their run is already terminal.  A RUNNING task
   in the claim-to-spawn window with no PID yet counts as a reservation.
4. If occupied slots are at capacity, return `resource_busy` and leave the
   candidate READY/unclaimed.
5. Otherwise delegate to the existing edge dispatcher while still holding the
   admission lock.  The existing board lock, claim-first GitHub label
   transition, workspace resolution, spawn-failure breaker, and PR lifecycle
   rules remain authoritative.
6. Release the resource lock after the existing dispatcher has claimed/spawned
   (or declined) the task.  Durable task/run state then represents occupancy
   for the next admission check.

## Core claim and health integration

The enabled `h4v3-resource-scheduler` plugin installs the same resource gate at
both core claim boundaries: `claim_task` for READY work and
`claim_review_task` for autonomous REVIEW work. A full matched resource returns
the core sentinel (`None`) without changing the task or failure counter, and
records a bounded in-process `resource_busy` diagnostic. A verified terminal
same-task worker may be reaped before replacement admission; active or
unverifiable PIDs remain fail-closed, and PID reuse is never signalled.

The plugin also wraps the module attributes used by the dispatcher health
probes (`has_spawnable_ready` and `has_spawnable_review`). When every eligible
candidate is resource-busy, those probes report no spawnable work, so the
legacy six-tick `dispatcher stuck` warning is reserved for genuine spawn
failures. A queue containing any non-resource or available-resource candidate
continues to report spawnable work. The runtime dispatch result and CLI
`dispatch --dry-run` output expose `resource_busy` entries and do not present a
capacity-blocked candidate as a predicted spawn; dry-run performs no claim,
reap, or database write.

No resource configuration, empty configuration, or unmatched assignee keeps
the original core probe and dispatch behavior unchanged. Core source remains
untouched; all integrations are idempotent runtime overlays.

## Backwards compatibility

The overlay deliberately has three no-op paths:

- no `kanban.worker_resources` section;
- an empty section;
- pending task assignee matches no configured group.

In those cases `_dispatch_pending_rework()` is called directly without a
resource lock or cross-board scan.  Parallel-capable providers therefore keep
their existing behavior unless the operator explicitly places their profiles
in a constrained group.

The existing `kanban.max_in_progress` remains board-scoped and unchanged.
Resource capacity is an **additional edge rework admission constraint**, not a
replacement for the core scheduler.

## Scope boundary

This patch prevents the edge-owned rework bypass from creating a worker when a
configured shared resource is already occupied, and it reaps a terminal
same-task worker before the edge creates the replacement run.

It does **not** rewrite Hermes core's ordinary READY-task dispatcher.  A future
core-level provider/resource scheduler can use the same resource-group model,
but this control-plane repository keeps its documented non-goal of modifying
Hermes core.  The edge gate still observes ordinary core workers through their
durable run PIDs, so a normal worker already using the constrained resource
blocks a new edge rework spawn.

## Deployment

`automation/hermes/scripts/deploy-intake-edge.sh` deploys three edge files:

- `kanban-github-sync.py` — small live entrypoint;
- `kanban-github-sync-core.py` — unchanged canonical reconciliation source;
- `kanban_resource_admission.py` — the opt-in admission overlay.

The candidate-copy, compile, `--help` smoke, backup, atomic replace, hash
verification, and rollback protocol remains in force.

Example dry-run:

```bash
docker exec hermes-cloudcli-agent bash \
  /ws/projects/hermes-n8n-control-plane/automation/hermes/scripts/deploy-intake-edge.sh \
  --hermes-home /home/hermes/.hermes \
  --dry-run
```

## Validation

Repository test added by this change:

```bash
python3 edge/test-kanban-resource-admission.py
python3 edge/test-kanban-resource-busy-health.py
```

It covers:

- configuration absent -> legacy dispatcher delegates unchanged;
- unmatched/parallel profile -> delegates unchanged;
- capacity 1 blocks a matching live worker on a sibling board;
- capacity 2 admits the second worker;
- terminal same-task live worker is reaped before replacement admission;
- active same-task run is never killed;
- ambiguous resource mapping fails closed.

The existing full rework regression suite must also remain green:

```bash
/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py
```

GitHub-hosted Actions are intentionally disabled for this repository, so these
commands are host/local validation gates rather than GitHub CI checks.
