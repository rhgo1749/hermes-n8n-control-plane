# REQ-032 v2: rework head-binding rejection feedback

- Status: implementation in progress / review pending
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Integration target: latest `main` after PR #36
- Work branch: `fix/head-binding-feedback-v2`
- Source issue: #32
- Supersedes: closed/unmerged PR #33
- Merge authority: human/user only

## Objective

Keep PR #33's useful behavior — visible, idempotent feedback for head-binding delivery rejection — while preserving PR #36's verification-only same-head retry semantics.

## Current invariant after PR #36

A same-head completion is NOT universally invalid.

- Ordinary rework round: `marker head == round-requested head` remains `rework_head_unchanged` and fails closed.
- Trusted maintainer retry (`trigger=maintainer_retry`, durable `retry_comment_id`) may complete verification-only at the same live head when the current round's finished run attests that exact head. That path is accepted delivery and MUST NOT emit head-advance feedback.
- `run_head_mismatch` means the marker/live PR head cannot be bound to the current worker run. It does not by itself prove that another source-code commit is required.

## Required behavior

### 1. `rework_head_unchanged`

Only when `_rework_delivery_evidence` actually rejects the round with this reason, post one idempotent `HERMES_KANBAN_REWORK_ATTENTION` PR comment for `(task, reason)`.

Guidance:
- name the round-requested head;
- explain that this ordinary round requires a worker-owned bounded commit that advances the PR head;
- tell the worker to validate/push the new live head and re-post `AGENT_REWORK_COMPLETE` with that head;
- do not alter retry/state routing.

### 2. `run_head_mismatch`

Post one idempotent attention comment, but DO NOT blindly require a new source commit.

Guidance:
- marker head must equal the live PR head;
- the current round's finished worker run summary/metadata must attest the same full head;
- if the live head already contains the requested fix, a trusted maintainer may open a fresh `AGENT_REWORK_RETRY` verification-only round and the worker may re-validate/attest that same head under PR #36;
- if the worker actually still owes code changes, it should create/push a bounded commit normally.

### 3. No warning on accepted verification-only delivery

The PR #36 success case must remain untouched: same-head + maintainer retry + retry identity + current-run head attestation -> accepted delivery (`verification_only=true`) with no head-binding warning.

## Implementation constraints

- Edge only; no Hermes core change.
- Preserve PR #36 `_rework_delivery_evidence` trust boundary and `review_requested` hold behavior.
- Preserve retry/state/label routing; feedback is observational only.
- Feedback posting failure degrades to the existing state machine and must not abort reconciliation.
- Idempotency remains `(task_id, diagnostic reason)`.

## Regression contract

Add focused coverage for at least:

1. ordinary same-head rejection -> one head-advance-specific feedback comment; repeated reconciliation -> no duplicate;
2. run-head mismatch -> attestation-specific feedback, with no unconditional "must create a new commit" claim;
3. accepted PR #36 verification-only same-head delivery -> no head-binding warning;
4. existing retry routing/result remains unchanged by feedback success/failure.

Run the full edge rework suite on the updated branch, plus syntax/diff checks. Do not reuse PR #33's stale `596 passed` result as current evidence.

## Deployment

After human merge, deploy only from merged `main` with the repository's canonical deploy script and verify the installed core copy matches merged source. Do not change the preserved Hermes job definition/schedule/enabled state.
