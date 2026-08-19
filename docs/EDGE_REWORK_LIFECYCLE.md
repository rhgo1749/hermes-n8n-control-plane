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
| `agent-rework` → `agent-working` | dispatcher `claim_task` success | atomic PATCH `labels: [-agent-rework, +agent-working]`; claim released + request label kept if the patch fails |
| `agent-working` maintained | running task with live claim/run | per-pass label projection (idempotent) |
| `agent-working` → `agent-review-ready` | delivery evidence complete (marker + head + validation) | DB `→ review` (done/blocked/ready/running sources), label swap, one `github_pr_rework_delivery` event (idempotent by head) |
| `agent-review-ready` maintained on a running claim | delivered round + running card (core review lane claim) | keep `running`; labels stay `agent-review-ready` (never `agent-working`) |
| `DONE + OPEN PR` repair | delivered round + card re-completed by a worker/reviewer while the PR is OPEN | classic `apply_decision` DONE `→` REVIEW (`github_pr_sync` event, assignee/claim/completed_at cleared) + labels `→ agent-review-ready`; dry-run predicts `repair_predicted: done_open_pr_repaired` |
| `agent-working` → `agent-rework` (safe retry) | worker crash / run failure / head mismatch / no marker, no human-attention text | task requeued `→ ready`, `github_pr_rework_retry` event, failure counted against `kanban.failure_limit` (circuit breaker preserved); head-binding rejection additionally posts idempotent reason-aware PR feedback without changing routing |
| `agent-rework` restored (BLOCKED hold) → new round | explicit trusted `AGENT_REWORK_RETRY` comment after the last attention, Issue open + `agent-ready`, comment id never consumed | `apply_rework` BLOCKED → READY + one fresh `github_pr_rework` event (`trigger: maintainer_retry`, `retry_comment_id`), delivery contract comment bound to the retry comment id; label kept until the dispatch claim (see “Explicit maintainer retry”) |
| `agent-working` → `agent-rework` + attention | ambiguous: completion marker missing / malformed / no run, or worker text asks for human input | labels restored, idempotent `HERMES_KANBAN_REWORK_ATTENTION` comment on the PR + Kanban `github_pr_rework_attention` event (per task + reason); the PR comment body carries the exact regeneration/`AGENT_REWORK_RETRY` instructions and reason-specific head-binding guidance where applicable |
| labels removed | PR merged | cleanup + classic REVIEW→DONE transition in the same pass |

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
   repeated reconciliation passes are no-ops after the transition.
7. `_current_round_delivery` consumes only durable `github_pr_rework_delivery`
   events that were emitted after `_rework_delivery_evidence` accepted the
   worker run + trusted completion marker + live-head evidence. It never turns
   an unvalidated raw PR comment into delivery evidence; the durable event is
   the already-validated boundary used by the active core-review fast path.
8. When a fresh `agent-rework` request arrives while stale
   `agent-review-ready` is still present, normalization removes the stale
   review-ready label, refetches the live labels, and continues the new round's
   REVIEW → READY evaluation in the **same reconciliation pass**. If that
   refetch fails, reconciliation stops fail-closed until the next wake.
9. All GitHub label mutations are a single atomic PATCH with read-back
   verification; failures fail closed (task state preserved).
10. Head-binding feedback is observational only. Posting failure is logged and
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
BLOCKED + `rework_human_attention` + the `agent-rework` label restored by
the self-heal. The restored label alone is **never** retry evidence, and no
automatic or timer-based retry may start from that state.

The **only** signal that opens a NEW rework round from the hold is a
machine-readable comment on the current PR by a `TRUSTED_GITHUB_ACTORS`
maintainer:

```text
AGENT_REWORK_RETRY
task=<task_id>
```

Acceptance requires ALL of:

- author in `TRUSTED_GITHUB_ACTORS`;
- exact `AGENT_REWORK_RETRY` line plus an exact `task=<task_id>` binding;
- comment `created_at` strictly AFTER the later of the governing rework
  event and the last `github_pr_rework_attention` record;
- the comment id was never consumed before;
- source Issue still OPEN with `agent-ready`.

When accepted, `apply_rework` writes exactly one fresh `github_pr_rework`
event with `trigger: "maintainer_retry"` (+ `retry_comment_id`), the task goes
BLOCKED → READY, and the edge dispatch lane claims it like a label-requested
round. For this trusted retry only, if the requested fixes are already present
at the live head, a same-head completion is valid **only when** the marker is
bound to the retry comment and the current round's finished worker run attests
that exact live head. Delivery evidence then records `verification_only: true`.

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
