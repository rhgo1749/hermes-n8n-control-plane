# PR Rework Worker Lifecycle State (edge reconciliation)

Durable design record for the GitHub-visible PR rework lifecycle projected by
the legacy edge reconciliation script (`edge/kanban-github-sync.py`).

## Label meanings

The five `agent-*` labels are the shared GitHub surface contract across
`hermes-agent`-topic repositories (Issue vs PR scope is part of the
contract and enforced by the sync):

| Label | Scope | Meaning |
|---|---|---|
| `agent-ready` | Issues only | Hermes가 이 Issue를 자동 intake/작업해도 된다는 지속적 작업 허가. PR에는 붙이지 않음. |
| `agent-rework` | Pull Requests only | maintainer가 이 PR을 다시 작업하라는 1회성 명령. Issue에는 붙이지 않음. |
| `agent-working` | Pull Requests only | Hermes가 이 PR의 rework를 소유하고 작업 중인 상태. 완료 시 agent-review-ready로 전환. |
| `agent-review-ready` | Pull Requests only | Hermes의 rework 배송이 완료되어 maintainer의 리뷰/머지를 기다리는 상태. |
| `agent-blocked` | Issues only | 작업이 maintainer의 결정·입력·권한을 기다리는 상태. 해결 답변 후 제거하면 재개 가능. PR에는 붙이지 않음. |

## Goal

Close the visibility gap where a rework worker owns a Kanban task while the
GitHub PR shows no work label at all (a human merged the PR during that
window and the worker's follow-up changes never reached `main`).

GitHub stays the human review surface; the lifecycle labels make Hermes
Kanban ownership observable on the PR. Kanban remains the execution
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
- `agent-working` is kept for the whole worker lifetime. It is **not**
  removed when the worker simply exits; it is removed only by the delivery
  or recovery transitions below.
- `agent-review-ready` requires ALL of: finished worker run, PR head equals
  the completion-marker head, `validation=passed`, trusted
  `AGENT_REWORK_COMPLETE` comment on the PR (machine-readable provenance),
  and — unless the round is a **verification-only maintainer retry** (see
  below) — the marker head is not the pre-rework requested head.
- `agent-review-ready` is **not** Kanban DONE. DONE only follows a fresh
  GitHub read proving the PR merged into the target branch.
- Worker completion is never a DONE ground for an OPEN PR. The core review
  lane may claim a delivered card (`review -> running`) and a reviewer may
  complete it (`done`) while the PR is still OPEN; the edge reconciliation
  repairs `DONE + OPEN PR` back to REVIEW on the next tick (see below).
- A running claim whose round is ALREADY delivered is the core review lane,
  never a rework owner: the label projection keeps `agent-review-ready` and
  never downgrades to `agent-working`.

## Transitions implemented

| Transition | Trigger | Effect |
|---|---|---|
| `agent-rework` → `agent-working` | dispatcher `claim_task` success | atomic PATCH `labels: [-agent-rework, +agent-working]`; claim is reclaimed and request label restored if claim-time projection fails |
| `agent-working` maintained | running task with live claim/run | per-pass label projection (idempotent) |
| `agent-working` → `agent-review-ready` | delivery evidence complete (marker + head + validation) | DB `→ review` (done/blocked/ready/running sources), label swap, one `github_pr_rework_delivery` event (idempotent by head) |
| `agent-review-ready` maintained on a running claim | delivered round + running card (core review lane claim) | keep `running`; labels stay `agent-review-ready` (never `agent-working`) |
| `DONE + OPEN PR` repair | delivered round + card re-completed by a worker/reviewer while the PR is OPEN | classic `apply_decision` DONE `→` REVIEW (`github_pr_sync` event, assignee/claim/completed_at cleared) + labels `→ agent-review-ready`; dry-run predicts `repair_predicted: done_open_pr_repaired` |
| `agent-working` → `agent-rework` (safe retry) | worker crash / run failure / head mismatch / no marker, no human-attention text | task requeued `→ ready`, `github_pr_rework_retry` event, failure counted against `kanban.failure_limit` (circuit breaker preserved); head-binding rejection additionally posts idempotent reason-aware PR feedback without changing routing |
| `agent-rework` restored (BLOCKED or operator-recovered REVIEW attention hold) → new round | explicit trusted exact `AGENT_REWORK_RETRY` whole-comment after the current round's attention, Issue open + `agent-ready`, comment id never consumed | the existing `apply_rework` transaction opens READY from the actual prior status and records one fresh `github_pr_rework` event (`trigger: maintainer_retry`, `retry_comment_id`); the label stays until the existing dispatch claim (see “Explicit maintainer retry”) |
| `agent-working` → `agent-rework` + attention | ambiguous: completion marker missing / malformed / no run, or worker text asks for human input | labels restored, idempotent `HERMES_KANBAN_REWORK_ATTENTION` comment on the PR + Kanban `github_pr_rework_attention` event (per task + reason); the PR comment body carries the exact regeneration/`AGENT_REWORK_RETRY` instructions and reason-specific head-binding guidance where applicable |
| labels removed | PR merged | cleanup + classic REVIEW→DONE transition in the same pass |

## Ordering / race safety

1. `claim_task` first, GitHub labels second (never the reverse).
2. `agent-working` present ⇒ no new rework spawn (`working_label_present`).
3. Another **running Kanban task owning the same PR** ⇒ no spawn
   (`pr_worker_active`, plus the existing board `max_in_progress` cap).
4. `agent-rework` + `agent-working` simultaneously:
   - while the Kanban task has a live active worker claim, defer with
     `lifecycle_conflict_deferred_active_worker`; keep both labels untouched so
     the worker's ownership remains observable and the newer request is not
     swallowed;
   - after the worker run ends, remove stale `agent-working`, preserve the
     newer `agent-rework`, and let the normal `REVIEW -> READY` intake consume
     it;
   - any other ambiguous lifecycle combination still skips with a diagnostic
     (`lifecycle_label_conflict`) and never spawns.
5. Classic intake (`REVIEW/BLOCKED` + fresh `agent-rework` label), plus a
   false-terminal `DONE + OPEN PR` card with a fresh trusted
   `AGENT_REWORK_RETRY`, flows through the bounded `DONE -> REVIEW -> READY`
   path. A label **newer than the governing event** always flows through the
   classic path.
6. A consumed round with current-round `github_pr_rework_attention` that an
   operator recovers to `REVIEW` may use the explicit retry admission only
   while the stale `agent-rework` label is present. Normal `review`/
   `agent-review-ready` lanes, a missing attention record, an active worker,
   and any lifecycle-label conflict stay outside that admission and fail
   closed without a new round.
7. Delivery events are written once per head (`_latest_delivery_head`), so
   repeated reconciliation passes are no-ops after the transition.
8. `_current_round_delivery` consumes only durable `github_pr_rework_delivery`
   events that were emitted after `_rework_delivery_evidence` accepted the
   worker run + trusted completion marker + live-head evidence. It never turns
   an unvalidated raw PR comment into delivery evidence; the durable event is
   the already-validated boundary used by the active core-review fast path.
9. When a fresh `agent-rework` request arrives while stale
   `agent-review-ready` is still present, normalization removes the stale
   review-ready label, refetches the live labels, and continues the new round's
   REVIEW → READY evaluation in the **same reconciliation pass**. If that
   refetch fails, reconciliation stops fail-closed until the next wake. The
   normalization baseline is either the prior `github_pr_rework_delivery` event
   or an explicit `github_pr_sync` event proving `DONE → REVIEW` for the same PR;
   missing or ambiguous timestamp/PR evidence still fails closed.
10. All GitHub label mutations are a single atomic PATCH with read-back
    verification. A claim-time PATCH non-2xx or stale read-back is recorded as
    one structured `github_pr_rework_projection_failure` event with the task,
    repository/Issue/PR, `stage=claim`, `operation=lifecycle_label_projection`,
    failure class, HTTP status when available, before/desired/observed labels,
    and `retryable=true`; the claim is reclaimed, `agent-rework` is restored,
    and no worker is spawned in that tick.
11. A retryable claim-projection failure remains the rework dispatch gate for a
    later edge wake. The later wake performs one fresh claim/projection attempt;
    a successful swap is the only ownership transition and does not duplicate
    the rework event or worker spawn. Repeated failures are bounded per wake and
    never spin in the same tick.
12. Head-binding feedback is observational only. Posting failure is logged and
    the canonical retry/hold result is returned unchanged.

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

### Worker pre-post checklist (mandatory)

Before posting `AGENT_REWORK_COMPLETE`, verify ALL of the following — a
comment that merely contains the marker string without these exact
key=value lines is REJECTED:

- [ ] first line is exactly `AGENT_REWORK_COMPLETE` — no suffix, no
      parenthetical, no surrounding text on that line;
- [ ] `task=<task_id>` matches the Kanban task id of the current round;
- [ ] `request_comment=<github_comment_id | none>` — the retry comment id
      when the round was opened by `AGENT_REWORK_RETRY`, else `none`;
- [ ] `head=<full 40-char SHA>` — exactly 40 lowercase hex characters AND
      equal to the live remote PR head (`git rev-parse HEAD` == remote PR
      head, verified by a fresh GitHub read);
- [ ] ordinary rework rounds advance beyond the round-requested `head_sha`.
      Same-head remains `rework_head_unchanged` **except** the PR #36 trusted
      maintainer verification-only path described below;
- [ ] the current round's finished worker run summary/metadata attests the
      same full SHA as the marker/live PR head. `run_head_mismatch` is an
      attestation failure and does not by itself prove another source commit
      is required;
- [ ] `validation=passed` exactly;
- [ ] the comment is posted by a `TRUSTED_GITHUB_ACTORS` author after the
      round event.

### PR attention feedback comment

The Kanban-side attention record alone is invisible on GitHub. Whenever a
delivery is rejected (`completion_handoff_missing`,
`completion_marker_malformed`, provenance failures, or a head-binding
failure), the edge posts ONE idempotent machine-readable comment on the PR
(per task + reason):

```text
HERMES_KANBAN_REWORK_ATTENTION
task=<task_id>
reason=<diagnostic>
```

The comment body always carries the exact `AGENT_REWORK_COMPLETE`
regeneration template and the `AGENT_REWORK_RETRY` instruction. For
`completion_marker_malformed` it additionally lists the missing/invalid
fields. For the two head-binding diagnostics the guidance is deliberately
different:

- `rework_head_unchanged`: this is an **ordinary-round head-advance** failure.
  The feedback names the round-requested head and tells the worker to create a
  bounded worker-owned commit, push/validate the new live PR head, and re-post
  the completion marker at that new head.
- `run_head_mismatch`: this is a **worker-run attestation** failure. The marker
  head must equal the live PR head and the current finished worker run must
  attest that same full SHA. A new source commit is **not inherently required**.
  If the live head already contains the requested fix, a trusted maintainer can
  open a fresh `AGENT_REWORK_RETRY` verification-only round and the worker can
  re-validate/attest that same head.

The feedback overlay is installed only **after** the canonical PR #36 delivery
validator. Therefore an accepted verification-only same-head delivery never
emits a head-binding warning: acceptance remains authoritative in
`_rework_delivery_evidence` and proceeds directly to `agent-review-ready`.
Feedback posting never changes state, labels, failure limits, or retry routing.

## Explicit maintainer retry (new round ingress)

A consumed rework round whose delivery is incomplete fails closed:
`BLOCKED` + `rework_human_attention` + the `agent-rework` label restored by
the self-heal. If an operator later recovers that same lifecycle card from
`DONE` to ordinary `REVIEW`, the current-round attention record and stale
label remain the bounded recovery evidence. The restored label alone is
**never** retry evidence, and no automatic or timer-based retry may start
from either held state.

The **only** signal that opens a NEW rework round from the hold is a
machine-readable comment on the current PR by a `TRUSTED_GITHUB_ACTORS`
maintainer:

```text
AGENT_REWORK_RETRY
task=<task_id>
```

Acceptance requires ALL of:

- author in `TRUSTED_GITHUB_ACTORS`;
- the whole non-empty comment is exactly the two lines
  `AGENT_REWORK_RETRY` and `task=<task_id>` (surrounding whitespace is
  ignored; extra prose is rejected);
- comment `created_at` strictly AFTER the later of the governing rework
  event and the last `github_pr_rework_attention` record;
- the comment id was never consumed before;
- source Issue still OPEN with `agent-ready`.

When accepted, the existing `apply_rework` transaction writes exactly one
fresh `github_pr_rework` event with `trigger: "maintainer_retry"` (+
`retry_comment_id`), and the task goes from its actual held state
(`BLOCKED` or recovered `REVIEW`) to `READY`. The event's
`previous_status` records that source state. The edge dispatch lane then
claims it like a label-requested round. For this trusted retry only, if the
requested fixes are already present at the live head, a same-head completion
is valid **only when** the marker is bound to the retry comment and the
current round's finished worker run attests that exact live head. Delivery
evidence then records `verification_only: true`.

### Operator-recovered `REVIEW` admission (Option A)

This is the canonical recovery policy for Issue #57. A recovered `REVIEW`
card is eligible only when the latest consumed rework event has a matching
current-round `github_pr_rework_attention` record, the linked PR is still
open, and the live PR retains only the request-side `agent-rework` lifecycle
label. A fresh trusted exact retry comment is consumed by the same
`_consume_explicit_rework_retry()` helper and the same `apply_rework(...,
retry_comment_id=...)` transaction used by the `BLOCKED` path. The next
dispatch tick uses the existing rework dispatch lane and swaps
`agent-rework` → `agent-working` only after claim.

Normal `REVIEW` / `agent-review-ready` cards, label-only state, stale or
untrusted comments, wrong-task or malformed comments, already-consumed
comment ids, active workers, and lifecycle-label conflicts remain
fail-closed: the card stays in its current state, no duplicate
`github_pr_rework` event is written, and no worker is spawned. Option B —
forcing recovered `REVIEW` back to `BLOCKED` — is intentionally rejected so
the normal review lane does not acquire a new state transition.

## Terminal merge convergence of a stale rework graph (Issue #73)

A merged GitHub PR is an authoritative fact, but the internal dependency
gate holds the intake root open while ANY reachable node is still
non-terminal -- including *stale* blocked or unstarted rework/reviewer
nodes whose work was already delivered and merged on GitHub (the H4V3-DJ
#88 topology: done implementation -> blocked rework -> todo reviewer ->
todo intake root).  The edge therefore converges such a graph in one
reconciliation pass, and ONLY when the full authority chain is freshly
proven in the same pass:

1. the card is a canonical GitHub Issue intake root;
2. a fresh GitHub read reports the source Issue `closed`;
3. every required linked PR is freshly read as closed, merged, and
   targeting the configured target branch;
4. the reachable dependency-chain nodes are unambiguous (every ancestor
   is terminal, a stale `blocked` node, or an unstarted
   `todo`/`review`/`ready`/`scheduled` node) and none has an active
   claim/run/worker.
5. every mutable `blocked` ancestor has a durable edge-owned
   `github_pr_rework`/retry event whose repository, Issue, PR, round, and
   full head SHA match the fresh merged-PR evidence;
6. every mutable allowed-status ancestor has a durable `created` role of
   `kanban-reviewer`, and its recorded parent set proves that it is the
   reviewer/waiting node for the stale rework round. Status, title, body, or
   a matching Issue/PR alone is never sufficient.

The blocked-node predicate is round-aware.  A stale `blocked` row's existing
`block_kind` is not enough to refuse the original #88-shaped convergence, but
the selected rework event must first still be the node's current governing
transition.  The edge proves that with the canonical
`_REWORK_GOVERNING_KINDS` ordering `(created_at, id)`; any later
status-affecting governing event (`github_pr_sync`, `completed`, `status`,
`promoted`, `unblocked`, `reclaimed`, `scheduled`, `archived`, the retryable
`github_pr_rework_projection_failure` claim diagnostic, or another canonical
transition) supersedes the old round and blocks terminalization.

Any later canonical `blocked` event is an explicit worker/operator hold and
blocks terminalization.  A later `github_pr_rework_attention` event blocks it
when its repository, Issue, PR, and `rework_round` match the governing round;
if that later attention record is malformed or has mismatched identity, the
evidence is ambiguous and also fails closed rather than being ignored.  Both
holds and ambiguous records preserve the blocked node's metadata and all
durable events.  An old attention record from an earlier round cannot suppress
a newer valid rework round because it precedes the newer governing event.

The single transaction then terminalizes the graph: the stale `blocked`
implementation/rework node becomes `done` with explicit GitHub-merge
provenance (the merge is the authoritative record that the work was
delivered), the unstarted reviewer/waiting node becomes `archived` (never
a fabricated reviewer PASS), and the intake root projects to
authoritative `done` -- each with a durable `github_pr_sync` event
(`reason: terminal_merge_convergence`, merged-PR provenance,
`merge_authority: human`, `auto_merge: false`) and stale claim/block
fields cleared.  This mutation is not taken when the current round has a
later human block or matching attention hold.  A second pass is a no-op, and
no worker is ever promoted, claimed, spawned, or re-run.

After the fresh GitHub evidence is accepted, the edge acquires a write
transaction and re-reads the complete reachable node and edge closure before
the first mutation.  Any status/ownership change, late parent or removed edge,
dangling link, or closure cycle is a fail-closed refusal; the transaction
preserves every node and event.  The final root write also re-evaluates the
direct-parent terminal predicate in the same transaction, so a late active
parent cannot bypass the dependency gate.  Ancestor traversal uses an active
path cycle check while still skipping completed shared ancestors, so diamond
graphs remain valid and bounded.

Fail-closed boundaries: open Issue, an open or closed-unmerged required
PR, a non-authoritative/failing GitHub read, any active claim/run/worker
ownership, missing/mismatched rework or reviewer provenance, a rework event
superseded by a later canonical governing transition, or an
unrelated/ambiguous ancestor, a later explicit human `blocked` event, or a
later matching or malformed/mismatched current-round
`github_pr_rework_attention` event preserves the graph unchanged (the classic
`internal_dependency_pending` lane keeps the root runnable).  Comment/run
lookup failure is also fail-closed: the edge returns
`text_source_lookup_failed` instead of treating incomplete handoff text as
an empty source set. This is not a generic dependency bypass: the gate still
protects active work, and the classic lane remains the owner of every
non-qualifying shape.  A mutating convergence entry is
`reason: terminal_merge_convergence` (root/blocked) or
`terminal_merge_convergence_archived_unstarted` (reviewer); dry-run predicts
with `terminal_merge_convergence_predicted`. Refusals report
`terminal_convergence_active_ownership`,
`terminal_convergence_node_unconvergeable`,
`terminal_convergence_ambiguous_graph`, or
`text_source_lookup_failed` without mutation.

`edge/test-kanban-github-sync-terminal-convergence.py` pins the #88
topology convergence, repeat-pass idempotence without claim/spawn, the
issue-open / open-PR / closed-unmerged-PR / GitHub-error preservation
matrix, active-ownership refusal, unrelated blocked/allowed-status ancestor
refusal, missing rework provenance, comments/runs lookup failure with
incomplete PR text, late ancestor activation and late active-parent insertion
during the GitHub read, bounded cycle refusal, and diamond/shared-ancestor
traversal.  Later durable human-block and current-round attention holds,
superseded governing transitions, malformed/mismatched later attention, and
earlier-round attention before a newer valid rework are also regression-tested
to preserve the blocked node and root dependency gate without weakening the
original positive convergence path.

## Deployment (host)

The authoritative intake job remains Hermes job `default:bf431b2a6ba6`, but
after PR #35 it is kept paused between **event-driven / async-only** wakes;
n8n no longer owns a five-minute polling schedule for GitHub intake. The
canonical reconciliation source remains `edge/kanban-github-sync.py`.

Deploy through `automation/hermes/scripts/deploy-intake-edge.sh`. The deploy
script installs the canonical reconciliation source as
`$HERMES_HOME/scripts/kanban-github-sync-core.py`; the historical live path
`$HERMES_HOME/scripts/kanban-github-sync.py` is the overlay entrypoint. It
installs both `kanban_resource_admission.py` and
`kanban_head_binding_feedback.py` onto the loaded canonical core. All overlay
dependencies and the core are candidate-compiled and installed before the
entrypoint switch, then byte-for-byte hash-verified. Deployment never edits
the preserved Hermes job definition, schedule, or enabled state. This PR does
**not** auto-deploy.

## Verification

`/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py`
remains the canonical lifecycle matrix. Tests 106–115 pin the PR #36 hardened
seams: genuine no-op same-head remains rejected (106), trusted maintainer
verification-only same-head delivery is accepted (107), stale historical
markers cannot close a newer round (108), `review_requested` becomes a human
hold without auto-requeue (109), ordinary crashes still requeue (110), retry
comments stay one-shot (111), malformed markers fail closed (112), claim
failure remains recoverable (113), and stale review-ready normalization
continues the new round in the same pass while remaining idempotent (114–115).
Tests 116–120 pin the Issue #57 operator-recovered `REVIEW` admission, exact
whole-comment parsing, stale/invalid retry fail-closed behavior, normal
review-lane isolation, lifecycle-label conflict guard, and the existing
classic fresh-label path. Tests 125–127 pin claim-time PATCH/read-back failure
classification and durable evidence, READY + `agent-rework` preservation with
one later retry/spawn, and `github_pr_sync`-provenance stale review-ready
normalization before claim.

`/ws/hermes-agent/venv/bin/python3 edge/test-kanban-head-binding-feedback.py`
installs the production head-binding overlay, first exercises focused
regressions for ordinary same-head feedback, run-head attestation feedback,
and accepted verification-only same-head delivery with **no warning**, then
runs the complete existing rework harness under the overlay. This makes the
new PR-feedback behavior prove compatibility against every pre-existing
lifecycle regression rather than using a stale pre-#36 baseline count.

Hermes core (`kanban_db.py`, tools, CLI) is untouched; all state mutations
remain in the canonical edge reconciliation scope. The new head-binding layer
is feedback-only.
