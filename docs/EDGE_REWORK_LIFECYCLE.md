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
| `agent-rework` restored (BLOCKED hold) → new round | explicit trusted `AGENT_REWORK_RETRY` comment after the last attention, Issue open + `agent-ready`, comment id never consumed | `apply_rework` BLOCKED → READY + one fresh `github_pr_rework` event (`trigger: maintainer_retry`, `retry_comment_id`), delivery contract comment bound to the retry comment id; label kept until the dispatch claim (see “Explicit maintainer retry”) |
| `agent-working` → `agent-rework` + attention | ambiguous: completion marker missing / no run, or worker text asks for human input | labels restored, `HERMES_KANBAN_REWORK_ATTENTION` comment + `github_pr_rework_attention` event (idempotent per round); the comment body carries the exact `AGENT_REWORK_RETRY` retry instructions |
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

## Explicit maintainer retry (new round ingress)

A consumed rework round whose delivery is incomplete fails closed:
BLOCKED + `rework_human_attention` + the `agent-rework` label restored by
the self-heal.  The restored label alone is **never** retry evidence (the
edge re-applies it during recovery, so label presence cannot distinguish a
human request from self-heal), and no automatic or timer-based retry may
start from that state.

The **only** signal that opens a NEW rework round from the hold is a
machine-readable comment on the current PR by a `TRUSTED_GITHUB_ACTORS`
maintainer:

```text
AGENT_REWORK_RETRY
task=<task_id>
```

Acceptance requires ALL of:

- author in `TRUSTED_GITHUB_ACTORS` (comment on the PR, trusted-actor check);
- exact `AGENT_REWORK_RETRY` line plus an exact `task=<task_id>` binding
  (no fuzzy natural-language matching);
- comment `created_at` strictly AFTER the later of the governing rework
  event and the last `github_pr_rework_attention` record;
- the comment id was never consumed before (each consumed retry comment id
  is recorded in the new round's `github_pr_rework` event payload as
  `retry_comment_id`, making it permanently ineligible);
- source Issue still OPEN with `agent-ready`.

When accepted, the held round is closed through the classic intake
contract: `apply_rework` writes exactly one fresh `github_pr_rework` event
with `trigger: "maintainer_retry"` (+ `retry_comment_id`), the task goes
BLOCKED → READY with the rework delivery contract comment (whose
`request_comment` is the retry comment id — the new round's completion
marker must reference it), and the edge dispatch lane claims it exactly
like a label-requested round (claim → `agent-working`).  A failed GitHub
lookup, closed Issue, missing `agent-ready`, malformed marker, untrusted
author, pre-attention timing, or already-consumed comment all fail closed:
the task stays BLOCKED.  Dry-run predicts `maintainer_retry_predicted`
without mutating anything.

```text
incomplete old delivery
  -> BLOCKED -> rework_human_attention -> agent-rework restored
  -> WAIT (no auto retry; label presence is never retry evidence)

explicit trusted retry comment (AGENT_REWORK_RETRY + task=..., after attention)
  -> old round closed
  -> fresh github_pr_rework event (trigger: maintainer_retry)
  -> BLOCKED -> READY -> claim -> agent-working  (new round)
```

## Deployment (host)

The live cron path is `~/.hermes/scripts/kanban-github-sync.py` driven by the
5-minute intake job (`bf431b2a6ba6`).  Deploy this tracked copy with the
candidate-copy protocol: backup → copy candidate → full regression suite →
cron-equivalent dry-run → atomic `mv` → post-replace verification.  This PR
does **not** auto-deploy and does **not** merge itself.

## Verification

`/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py`
covers the lifecycle matrix (554 checks): claim transition, claim failure,
duplicate-spawn guards (label + same-PR owner), working-label maintenance,
local-commit-only, head mismatch, validation incomplete, handoff failure,
full delivery, worker crash requeue, label conflict skip, merged cleanup,
the pre-existing rework/blocked/annotation regressions, the DONE + OPEN PR
invariants, the consumed-rework provenance fail-closed guard (PR #27 merge,
tests 80–82), and the explicit maintainer retry (AGENT_REWORK_RETRY) ingress
(tests 83–97; renumbered after PR #27 occupied 80–82).  Tests 63–70
pin the DONE + OPEN PR invariants: reviewer completion repair (63), rework
completion → REVIEW on the same PR (64), delivered + merged → DONE (65),
DONE + OPEN PR + stale `agent-working` self-heal (66, acceptance fixture
t_560e6a71 / PR #9), live-worker non-transition + post-delivery label
stability (67), generic READY + OPEN PR keeps the core `active_pr` guard
(68), repeated-tick idempotency (69), and dry-run repair prediction without
mutation (70).  Tests 80–82 pin the consumed-rework provenance fail-closed
guard: wrong-task completion marker stays blocked (80), unresolved
consumed-round `pr_number` stays blocked with attention (81), mismatched/
recreated PR stays blocked (82).  Tests 83–97 pin the explicit retry
contract: BLOCKED attention hold without auto READY (83), pre-attention
retry ignored (84), untrusted actor ignored (85), malformed/ambiguous retry
ignored (86), trusted retry → exactly one fresh `github_pr_rework` event +
READY + next-tick idempotency (87), dispatch claim → agent-working/RUNNING
(88), claim failure keeps the request (89), stale completion marker never a
delivery for the retry round (90), complete retry-round delivery → REVIEW
(91), blocked outcome + complete delivery → REVIEW (92), merged → DONE (93),
generic BLOCKED unaffected (94), dry-run prediction without mutation (95),
consumed retry comment permanently ineligible (96), and Issue open +
`agent-ready` requirement (97).

Hermes core (`kanban_db.py`, tools, CLI) is untouched; all mutations are edge
direct-DB writes inside the operator-approved reconciliation scope.
