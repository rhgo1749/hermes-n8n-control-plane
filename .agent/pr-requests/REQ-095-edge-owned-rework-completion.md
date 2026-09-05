# REQ-095: edge-owned AGENT_REWORK_COMPLETE 생성

- Status: Implementation complete; provisional handoff
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `EDGE_REWORK`, `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `issue95-edge-owned-rework-completion`
- Source-of-truth base: `origin/main` at `da0f83ba56cb5998f11f66a32ed4dc35e656a470`
- Source issue: `rhgo1749/hermes-n8n-control-plane#95`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/95
- Kanban task ID: `t_ef923f46` (intake root: `t_9d0d0d1b`)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:95`
- Implementation owner: `kanban-developer`
- Merge authority: Human/user only
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

Rework workers complete through the ordinary Kanban completion surface and durably
attest `validation=passed` plus the full validated `head_sha` in the current
`task_runs.metadata`. The canonical edge then re-reads the current round and
fresh GitHub state, constructs the exact trusted top-level
`AGENT_REWORK_COMPLETE` marker, reads it back, and only then projects
`agent-review-ready`.

## Confirmed ownership and constraints

- GitHub remains the durable PR conversation/audit record.
- Hermes Kanban remains the execution/run authority; `task_runs.metadata` is
  reused for the worker validation/head attestation.
- `edge/kanban-github-sync.py` remains the sole GitHub↔Kanban reconciliation
  writer for this lifecycle.
- `TRUSTED_GITHUB_ACTORS` remains `{ "rhgo1749" }`.
- Existing trusted worker/maintainer markers remain accepted unchanged for
  backward compatibility; the edge-created path is additive.
- Edge marker fields (`task`, `request_comment`, `head`, `validation`) are never
  copied from worker free text: task/round/request binding come from durable
  edge state, and head comes from a fresh PR read.
- No Hermes core, n8n workflow, second dispatcher, parallel state store,
  label-semantics, merge, auto-merge, or deployment changes.

## Edge creation contract

The edge posts at most one marker for the current `(task, round, head)` after
checking the single open target-branch PR, source Issue `open` + `agent-ready`,
current request binding, trusted authenticated edge actor, finished run
provenance, `validation=passed`, and run-head/live-head equality. A stale head,
round mismatch, missing/untrusted request, failed read, untrusted actor, failed
POST, or failed read-back fails closed without delivery projection. Existing
round/timestamp isolation and delivery-event idempotence remain in force.

## Changed files

- `edge/kanban-github-sync.py`
  - Add additive edge-owned marker creation/read-back path.
  - Keep existing marker consumption and retry signal semantics.
  - Update generated worker contract to attest metadata and not post the marker.
- `edge/kanban_head_binding_feedback.py`
  - Keep the head-binding feedback overlay compatible with the delivery-evidence
    keyword arguments and dry-run mutation guard.
- `edge/test-kanban-github-sync-rework.py`
  - Add edge-created, stale-head, round-mismatch, duplicate-callback,
    old-round, backward-compatibility, untrusted-actor, and dry-run regressions.
- `docs/EDGE_REWORK_LIFECYCLE.md`
  - Record the edge-owned transition, fail-closed boundaries, and idempotence.

## Validation record

Required local gates (GitHub Actions are intentionally disabled):

- [x] `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py` — PASS (`916 passed, 0 failed`)
- [x] `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-head-binding-feedback.py` — PASS (`916 passed, 0 failed`, including overlay regressions)
- [x] `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-completion.py` — PASS (`ALL COMPLETION REGRESSIONS PASS`, 22 checks)
- [x] `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 -m py_compile edge/kanban-github-sync.py edge/kanban_head_binding_feedback.py edge/test-kanban-github-sync-rework.py` — PASS
- [x] `git diff --check` — PASS
- [x] Ruff added-line scan across all changed Python files — PASS (0 diagnostics on added lines); full-file Ruff remains non-clean on pre-existing baseline findings.

Host deployment/runtime validation is not performed by this repository worker;
deployment remains a separate human gate. No GitHub Actions check is treated as
local validation.

## Handoff / stop state

Implementation evidence is ready for PR delivery and human review. Do not merge
or auto-merge. The final automation stop is the human PR/host acceptance gate;
the authoritative edge may project terminal Kanban state only after a fresh
GitHub merge read.
