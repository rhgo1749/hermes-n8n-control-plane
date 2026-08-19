# REQ-044 · Kanban lifecycle ownership / no post-PR polling

- Source Issue: #44
- Source repository: `rhgo1749/hermes-n8n-control-plane`
- Base: `main` at `ff1c4daf3bce3aa3d05458a59fb64476994bd8c6`
- Delivery: existing GitHub-backed intake wrapper + canonical lifecycle docs

## Problem

A GitHub-backed worker can finish implementation, local validation, push, and PR creation, then remain alive while sleeping/polling for GitHub Actions or other future external state. A live PID can continue consuming a constrained worker-resource slot even though the useful implementation work is finished.

The current PR #41 completion overlay correctly uses core `kanban_complete` and lets the edge project an OPEN PR to parked `review`, but the generated lead execution contract does not explicitly forbid post-PR monitoring or separate Main/Controller/Developer/Reviewer/Designer ownership.

## Required change

1. Extend the deployed intake task overlay so generated GitHub-backed tasks explicitly forbid keeping an implementation/review worker RUNNING solely to wait for future CI, human review, merge, or comments.
2. Main uses real Kanban dependencies for waiting and does not poll running specialists.
3. Developer owns implementation + required local validation + PR delivery, then hands off.
4. Reviewer verifies the current immutable review surface/evidence and returns PASS/REWORK without becoming a monitor.
5. Designer remains product/UX decision/review only.
6. Deterministic Controller/edge owns queue/resource/lease/stale recovery and external GitHub projection.
7. Preserve the existing `kanban_complete` / provisional done / parked review / no `kanban_request_review` contract.
8. Add a source-controlled canonical role contract and regression coverage.

## Non-goals

- Hermes core scheduler rewrite.
- Automatic killing/reaping of arbitrary terminal worker PIDs.
- Changing worker resource capacity.
- Router/lease-controller redesign.
- PR #43 webhook delivery dedupe scope.
- GitHub Actions enablement or auto-merge.

## Validation

Required deterministic repository checks for this change:

- `python3 tests/test_intake_completion_contract_entrypoint.py`
- `python3 -m py_compile automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py tests/test_intake_completion_contract_entrypoint.py`
- `python3 automation/n8n/scripts/validate.py`
- `git diff --check`

Host deployment/canary remains external/manual and must not be represented as PASS unless actually run.

## Stop state

After required repository validation and PR creation/update, the implementation worker must hand off and terminate. Future CI/review/merge state is not a reason to keep that worker alive.