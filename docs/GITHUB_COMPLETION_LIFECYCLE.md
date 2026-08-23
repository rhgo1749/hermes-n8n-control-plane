# GitHub-backed Kanban completion lifecycle

GitHub-backed Issue intake cards use deliberately separate lifecycle authorities:

- **Hermes core** owns worker-run termination. A root worker whose required internal task graph is satisfied terminates its run with `kanban_complete`.
- **Kanban specialists** own bounded work products: Developer implements, Reviewer verifies, and Designer decides/reviews user-facing behavior when needed.
- **The control-plane Controller/edge** owns deterministic queue/resource/lease/stale recovery and GitHub completion projection. Fresh GitHub state determines whether the card is parked in `review` or is authoritatively `done`.

The durable role boundaries are defined in `docs/KANBAN_ROLE_CONTRACTS.md`.

The external event path is `github-router` (HMAC, delivery dedupe, and managed
repository admission) → private n8n Webhook (bounded PR-event filter) → the
loopback actuator → `kanban-github-sync.py --board <slug> --json`. The actuator
reuses repository-registry/task-provenance board authority and the existing
edge state machine; n8n does not write Kanban state directly. The tracked
workflow has no Schedule Trigger or polling fallback.

## Completion-side wake

Root worker completion deliberately remains a two-step projection:

```text
worker core kanban_complete
  -> committed provisional DONE + completion cleanup
  -> kanban_task_completed plugin observer (worker process)
  -> committed-row/board/runtime validation
  -> $HERMES_HOME/scripts/kanban-github-sync.py \
       --board <validated-slug> --json
       first bounded attempt
       timeout -> re-read committed task
         ├─ no longer DONE -> stop; prior owner already projected it
         └─ still DONE / uncertain read -> exactly one fresh-budget retry
  -> existing edge DONE -> REVIEW / REVIEW -> DONE decision
```

`hermes-plugin/github-completion-edge-wake/` is an observer/trigger only. It
reads the already-committed task row and wakes the deployed canonical/live edge
path when the importer-owned provenance contains `source: github-issue` or
`completion contract: github-pr` before the canonical Issue-body boundary.
Ordinary tasks, missing or ambiguous board/runtime evidence, and non-`DONE`
rows do not wake the edge. The observer never calls a Kanban mutator, parses the
untrusted Issue body as instructions, or duplicates the edge transition logic.

The actuator and this direct completion wake share the same edge single-flight
boundary. Before the canonical edge reads GitHub/Kanban state, it acquires the
guarded runtime lock at
`$HERMES_HOME/kanban/.resource-locks/github-edge-sync.lock` with Linux
`fcntl.flock(LOCK_EX)`. A completion wake waits behind an in-flight webhook or
another completion wake rather than running concurrently. Kernel lock ownership
releases on a crashed process, and no queue database, polling loop, or second
transition owner is introduced.

A single outer deadline is not allowed to turn lock contention into a lost
completion signal. The first completion child uses the normal bounded edge
budget. If that child times out—possibly because the preceding owner consumed
most of the deadline while the child was blocked in `flock()`—the observer does
not accept that timeout as the completion handoff. It re-reads the committed
task state. When the earlier owner already moved the card away from `DONE`, the
observer stops without launching a duplicate edge process. When the task is
still an eligible GitHub-backed `DONE`, or the post-timeout read itself fails
closed, the observer launches exactly one second fixed-argv child with a
completely fresh deadline. Only a second timeout becomes the final bounded
`edge_retry_timeout` diagnostic. Non-timeout failures are not retried. There is
no sleep, retry loop, Schedule Trigger, or polling fallback.

The process-level contention regression models the race directly: the owner
enters the canonical edge before the completion is committed, so its snapshot
cannot contain the later provisional `DONE`; the first completion attempt is
forced to expire behind that owner; the test passes only when the fresh retry
runs after the owner and observes the post-owner completion snapshot. A focused
unit contract also proves that an already-projected non-`DONE` row suppresses
the retry and that an uncertain post-timeout re-read does not consume the wake.
Merely recording a timeout diagnostic is explicitly not success evidence.

The callback uses a fixed argument vector with `shell=False`, bounded per-attempt
timeout/output budgets, and stable diagnostics that do not include task body,
summary, command output, or credentials. Both wake paths load the shared
`automation/hermes/edge_sync_timeout.py` contract at installation time:
`HERMES_EDGE_SYNC_TIMEOUT_SECONDS` defaults to `120` seconds and must be finite,
positive, and no greater than `3600`; the completion observer adds its bounded
`5`-second grace to each attempt. Combined child output is capped at `64 KiB`;
invalid timeout configuration or oversized output fails closed before any edge
success is reported.

A final failed wake is observable and fail-closed: the core completion remains
provisional `DONE` and is never treated as merge evidence. Under ordinary
contention, however, the first timeout is followed by committed-state
revalidation and, when necessary, the mandatory fresh retry rather than leaving
the card stranded. If the edge already moved the card, the observer suppresses
the duplicate run instead of relying only on downstream idempotency.

## Why worker `kanban_request_review` is forbidden here

A GitHub-backed worker already has an external review surface: its linked GitHub pull request. Calling core `kanban_request_review` creates a second internal review lane. With the same/default Kanban profile, that review card can be claimed again by the implementation worker, producing a `review -> running -> review` self-review loop while the PR is simply waiting for a human merge.

The intake wrapper therefore emits this contract for new GitHub-backed cards:

1. use real Kanban dependencies for specialist sequencing; Main does not stay RUNNING to poll a child worker;
2. Developer finishes implementation, required repository-local deterministic validation, and PR create/update, records the current PR/head plus any unavailable external/manual gate, then hands off;
3. Reviewer verifies the current PR/head/diff/evidence and returns PASS or REWORK; it does not wait for a future CI/merge event;
4. no implementation, review, or lead worker stays RUNNING solely to wait for GitHub Actions/checks, human review, merge, or future comments;
5. once the root card's required internal graph is satisfied, finish the root worker run with core `kanban_complete`;
6. treat that core `done` as provisional, not as GitHub merge evidence;
7. never call `kanban_request_review` for the GitHub-backed intake card;
8. let `kanban-github-sync` re-query GitHub and project `DONE + required PR OPEN/closed-unmerged -> REVIEW`;
9. the existing `DONE -> REVIEW` edge transition clears `assignee`, claim/worker metadata, blocker metadata, and `completed_at`, leaving a parked review card;
10. only a trusted rework signal may return that parked card to runnable work;
11. only fresh GitHub evidence may project `REVIEW -> DONE` authoritatively. Normally every effective linked required PR must be merged into the target branch. A historical `closed + unmerged` PR may be excluded only when the source Issue is closed, no linked PR remains open, and a newer linked PR with the exact same non-empty head ref is merged into the target branch. Different-head, ambiguous, or still-open lineages remain `review`.

A one-time snapshot of current CI/check state may be recorded in a handoff when relevant. Future external state is not a reason to keep a scarce worker process alive. `NOT RUN` remains distinct from `PASS`; an unavailable required external/manual gate must be reported honestly rather than waited on by polling.

GitHub lookup failures remain fail-closed. Hermes core is not modified.

## Deployment

`automation/hermes/scripts/deploy-intake-edge.sh` deploys:

- `github-agent-ready-kanban-intake.py` — small live wrapper;
- `github-agent-ready-kanban-intake-core.py` — canonical intake implementation;
- the existing edge wrapper/core/overlays and repository registry.

The canonical intake source remains `automation/hermes/scripts/github-agent-ready-kanban-intake.py`. The live wrapper fail-closed overlays two rendered blocks: the GitHub completion contract and the Kanban lead orchestration contract. If either canonical source block drifts unexpectedly, the wrapper refuses to emit an unverified lifecycle contract.

Install the completion observer separately on the Hermes runtime that starts
workers; this is a manual host activation gate, not an automatic repository
deployment step:

```bash
automation/hermes/scripts/install-github-completion-edge-wake.sh \
  --hermes-home "$HOME/.hermes"
```

The installer validates the candidate, atomically replaces the user plugin,
keeps a timestamped backup, enables only
`github-completion-edge-wake`, and prints an exact rollback command. Deploy the
edge runtime first with `deploy-intake-edge.sh`, then restart the existing
Hermes worker/dashboard supervisor so the plugin is loaded in worker
processes. Host installation, plugin activation/restart, and a signed live
canary remain separate `HOST_VALIDATION_REQUIRED` gates; repository tests do
not claim those operations were performed.