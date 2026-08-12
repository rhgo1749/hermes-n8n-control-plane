# PR Rework Worker Lifecycle State (edge reconciliation)

Durable design record for the GitHub-visible PR rework lifecycle projected by
the legacy edge reconciliation script (`edge/kanban-github-sync.py`).

## Goal

Close the visibility gap where a rework worker owns a Kanban task while the
GitHub PR shows no work label at all (a human merged the PR during that
window and the worker's follow-up changes never reached `main`).

GitHub stays the human review surface; the lifecycle labels make Hermes
Kanban ownership observable on the PR.  Kanban remains the execution
projection, and `agent-ready` (Issue-level work permission) is unchanged.

## State model

```text
agent-rework          rework request exists; not yet claimed by a Kanban worker
   │  Kanban claim_task succeeds
   ▼
agent-working         worker owns the rework (running task, live claim)
   │  implementation + commit + push + PR head update
   │  + required validation + completion handoff comment
   ▼
agent-review-ready    remote delivery complete; human review/merge remains
   │  human merges the PR
   ▼
PR merged  ──────────►  GitHub reconciliation → Kanban DONE
```

Rules:

- `agent-rework` is only removed when the edge dispatcher's `claim_task`
  succeeds and the PR labels are atomically swapped to `agent-working`
  (claim-first; a claim or label failure leaves the request intake-visible).
- `agent-working` is kept for the whole worker lifetime.  It is **not**
  removed when the worker simply exits; it is removed only by the delivery
  or recovery transitions below.
- `agent-review-ready` requires ALL of: finished worker run, PR head equals
  the completion-marker head, `validation=passed`, trusted
  `AGENT_REWORK_COMPLETE` comment on the PR (machine-readable provenance),
  and the marker head is not the pre-rework requested head.
- `agent-review-ready` is **not** Kanban DONE.  DONE only follows a fresh
  GitHub read proving the PR merged into the target branch.
- Worker completion is never a DONE ground for an OPEN PR.  The core review
  lane may claim a delivered card (`review -> running`) and a reviewer may
  complete it (`done`) while the PR is still OPEN; the edge reconciliation
  repairs `DONE + OPEN PR` back to REVIEW on the next tick (see below).
- A running claim whose round is ALREADY delivered is the core review lane,
  never a rework owner: the label projection keeps `agent-review-ready` and
  never downgrades to `agent-working` (the pre-fix projection re-added
  `agent-working` on the review claim, regressing the delivered label).

## Transitions implemented

| Transition | Trigger | Effect |
|---|---|---|
| `agent-rework` → `agent-working` | dispatcher `claim_task` success | atomic PATCH `labels: [-agent-rework, +agent-working]`; claim released + request label kept if the patch fails |
| `agent-working` maintained | running task with live claim/run | per-tick label projection (idempotent) |
| `agent-working` → `agent-review-ready` | delivery evidence complete (marker + head + validation) | DB `→ review` (done/blocked/ready/running sources), label swap, one `github_pr_rework_delivery` event (idempotent by head) |
| `agent-review-ready` maintained on a running claim | delivered round + running card (core review lane claim) | keep `running`; labels stay `agent-review-ready` (never `agent-working`) |
| `DONE + OPEN PR` repair | delivered round + card re-completed by a worker/reviewer while the PR is OPEN | classic `apply_decision` DONE `→` REVIEW (`github_pr_sync` event, assignee/claim/completed_at cleared) + labels `→ agent-review-ready`; dry-run predicts `repair_predicted: done_open_pr_repaired` |
| `agent-working` → `agent-rework` (safe retry) | worker crash / run failure / head mismatch / no marker, no human-attention text | task requeued `→ ready`, `github_pr_rework_retry` event, failure counted against `kanban.failure_limit` (circuit breaker preserved) |
| `agent-working` → `agent-rework` + attention | ambiguous: completion marker missing / no run, or worker text asks for human input | labels restored, `HERMES_KANBAN_REWORK_ATTENTION` comment + `github_pr_rework_attention` event (idempotent per round) |
| labels removed | PR merged | cleanup + classic REVIEW→DONE transition in the same tick |

## Ordering / race safety

1. `claim_task` first, GitHub labels second (never the reverse).
2. `agent-working` present ⇒ no new rework spawn (`working_label_present`).
3. Another **running Kanban task owning the same PR** ⇒ no spawn
   (`pr_worker_active`, plus the existing board `max_in_progress` cap).
4. `agent-rework` + `agent-working` simultaneously ⇒ skip with a diagnostic
   (`lifecycle_label_conflict`), never spawn.
5. Classic intake (`REVIEW/BLOCKED` + fresh `agent-rework` label) keeps the
   historical `apply_rework` path; a label **newer than the governing event**
   always flows through the classic `DONE → REVIEW → READY` path.
6. Delivery events are written once per head (`_latest_delivery_head`), so
   repeated 5-minute ticks are no-ops after the transition.
7. All GitHub label mutations are a single atomic PATCH with read-back
   verification; failures fail closed (task state preserved).

## Machine-readable completion handoff

Workers post the marker in their existing human-readable final report on the
PR (trusted actor only):

```text
AGENT_REWORK_COMPLETE
task=<task_id>
request_comment=<github_comment_id | none>
head=<full 40-char PR head SHA>
validation=passed
```

The rework contract (with the marker template) is appended to the task
comments when the rework round is consumed, so the next worker sees it
without any core change.

## Deployment (host)

The live cron path is `~/.hermes/scripts/kanban-github-sync.py` driven by the
5-minute intake job (`bf431b2a6ba6`).  Deploy this tracked copy with the
candidate-copy protocol: backup → copy candidate → full regression suite →
cron-equivalent dry-run → atomic `mv` → post-replace verification.  This PR
does **not** auto-deploy and does **not** merge itself.

## Verification

`/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py`
covers the lifecycle matrix (349 checks): claim transition, claim failure,
duplicate-spawn guards (label + same-PR owner), working-label maintenance,
local-commit-only, head mismatch, validation incomplete, handoff failure,
full delivery, worker crash requeue, label conflict skip, merged cleanup,
and the pre-existing rework/blocked/annotation regressions.  Tests 63–70
pin the DONE + OPEN PR invariants: reviewer completion repair (63), rework
completion → REVIEW on the same PR (64), delivered + merged → DONE (65),
DONE + OPEN PR + stale `agent-working` self-heal (66, acceptance fixture
t_560e6a71 / PR #9), live-worker non-transition + post-delivery label
stability (67), generic READY + OPEN PR keeps the core `active_pr` guard
(68), repeated-tick idempotency (69), and dry-run repair prediction without
mutation (70).

Hermes core (`kanban_db.py`, tools, CLI) is untouched; all mutations are edge
direct-DB writes inside the operator-approved reconciliation scope.
