# REQ-057: operator recovery 후 `REVIEW` rework retry admission

- Status: Implementation complete / review pending
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `fix/issue-57-operator-recovery-retry`
- Source-of-truth base: fetched `origin/main` at `d168ca5d659e247f88c1ff00c2291be4e2f6342b`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-057-operator-recovery-rework-retry.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#57`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/57`
- Root Kanban task: `t_7e686c79`
- Implementation Kanban task: `t_355a7bcf`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:57`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `NONE`

## 0. Canonical route

`AGENTS.md` → `README.md` → `docs/README.md` →
`docs/EDGE_REWORK_LIFECYCLE.md` → `edge/kanban-github-sync.py` →
`edge/test-kanban-github-sync-rework.py`.

The edge script remains the GitHub ↔ Kanban lifecycle owner. Hermes core,
n8n, Overview, and Telegram ownership are unchanged.

## 1. Objective

After an incomplete consumed PR-rework round enters
`rework_human_attention`, an operator may recover the same Kanban card from
`DONE` to the ordinary `REVIEW` lane. A fresh trusted maintainer comment with
the exact whole-comment contract below must still open exactly one new rework
round through the existing transaction and dispatch path:

```text
AGENT_REWORK_RETRY
task=<task_id>
```

The recovered card must move `REVIEW → READY`, preserve the existing PR and
human-only merge authority, then reach the existing claim/spawn path without
using the stale `agent-rework` label as authorization.

## 2. Confirmed cause and policy decision

- The canonical lifecycle context is the latest durable
  `github_pr_rework` event in `edge/kanban-github-sync.py`.
- `_consume_explicit_rework_retry()` was called only from the `BLOCKED`
  lifecycle branch. Once operator recovery set the card to `REVIEW`, the
  lifecycle branch returned without consuming the retry comment.
- `evaluate_rework()` then saw the old `agent-rework` label timestamp as stale
  relative to the consumed event and returned `rework_claim_pending`; no new
  event or worker was produced.
- **Option A is canonical:** permit the existing explicit retry admission
  from an operator-recovered `REVIEW` only when the current round has a
  matching `github_pr_rework_attention` record, the linked PR is open, the
  request-side label is present without a lifecycle conflict, the Issue is
  open with `agent-ready`, and the trusted exact retry comment is fresh and
  unconsumed.
- **Option B is rejected:** do not normalize recovered `REVIEW` back to
  `BLOCKED`; that would broaden state transitions and change normal review
  lane semantics.
- Retry consumption reuses `apply_rework(..., retry_comment_id=...)`, records
  `github_pr_rework` with `trigger: maintainer_retry` and
  `previous_status: review`, and leaves label replacement to the existing
  claim-first dispatch lane.

## 3. In scope

1. Narrow review-path admission in `edge/kanban-github-sync.py`, including
   current-round attention evidence and strict whole-comment retry parsing.
2. Regression coverage for valid recovered-`REVIEW` retry, dispatch/spawn and
   idempotency, stale/old/untrusted/malformed/wrong-task/already-consumed
   signals, label-only no-op, normal review-ready isolation, lifecycle-label
   conflict, and the classic fresh-label path.
3. Update `docs/EDGE_REWORK_LIFECYCLE.md` with Option A, rejected Option B,
   stale-label and review-lane semantics.
4. Keep this task-specific request current with validation and PR evidence.

## 4. Explicit non-goals

- Hermes core (`kanban_db.py`, tools, CLI), new DB/state store, or dispatcher
  replacement.
- n8n, H4V3 Overview, Telegram, host deployment, or runtime configuration.
- Automatic/timer/label-only retries, weak head/label recovery, or retry
  authorization from arbitrary review comments.
- New PR creation, PR replacement, merge/auto-merge, or changing human merge
  authority.
- GitHub-hosted Actions changes or unrelated cleanup.

## 5. Ownership and security gates

- Ownership impact: `NONE`; the existing edge reconciliation and dispatch
  owners remain authoritative.
- New state/store: `NO`; current `task_events`, task status, PR labels, and
  existing transaction are reused.
- Existing authority bypassed: `NO`.
- Security impact: `AFFECTED` only at the existing trusted GitHub comment
  boundary; exact actor, task, timestamp, Issue-label, PR-open, and one-shot
  guards remain mandatory.
- Auth/secret impact: `AFFECTED` at the existing GitHub API read/write path;
  no new credential or secret is introduced.
- Host/network/platform policy impact: `NONE` beyond current GitHub API use.
- Residual owner: human maintainer for the exact retry comment, reviewer for
  the resulting PR, and human/user for merge.

## 6. Acceptance and validation contract

| Behavior | Evidence |
|---|---|
| Valid recovered `REVIEW` retry opens one round | `github_pr_rework` with `previous_status=review`, `trigger=maintainer_retry`, retry ID; `REVIEW → READY` |
| Existing dispatch path is used | claim, one spawn, `agent-rework → agent-working` |
| Replay is idempotent | no second event, consumption, or spawn |
| Invalid/stale/untrusted/malformed/wrong-task/already-consumed signals fail closed | card remains `REVIEW`, no new rework event |
| Label-only retry is non-authorizing | no state/event/label mutation |
| Normal `agent-review-ready` lane is isolated | remains `REVIEW` with `agent-review-ready` |
| Lifecycle conflict is fail closed | `lifecycle_label_conflict`, no retry event |
| Classic fresh-label admission is unchanged | existing `REVIEW + agent-rework` path records a non-retry event |

Required local commands:

```bash
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-rework.py
python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py
bash -n automation/hermes/scripts/deploy-intake-edge.sh
git diff --check
```

`STATIC_UNIT` and `EDGE_REWORK` are repository-local gates. GitHub Actions are
intentionally disabled by repository policy and are not a substitute for these
commands. Host deploy/live GitHub canary validation is not part of this task.

## 7. Delivery and final report fields

- Base SHA: `d168ca5d659e247f88c1ff00c2291be4e2f6342b`
- Work branch: `fix/issue-57-operator-recovery-retry`
- Changed-file allowlist: `edge/kanban-github-sync.py`,
  `edge/test-kanban-github-sync-rework.py`, `docs/EDGE_REWORK_LIFECYCLE.md`,
  this request file.
- Baseline Edge harness before implementation: `647 passed, 0 failed`.
- Pre-fix sabotage run: the new recovered-`REVIEW` regressions produced
  `44 passed, 14 failed`, demonstrating that the old BLOCKED-only admission
  does not satisfy the new contract.
- Post-change Edge harness: `706 passed, 0 failed` from the exact canonical
  command above.
- Focused Issue #57 scenarios: `59 passed, 0 failed` (tests 116–120).
- Retry-signal guard: `5 passed`.
- Python compile: `PASS` for the changed edge/test Python files.
- Shell syntax: `PASS` for `automation/hermes/scripts/deploy-intake-edge.sh`.
- `git diff --check`: `PASS`.
- LSP: changed production edge files report `0 errors, 0 warnings, 0
  informations`; the full legacy harness retains five pre-existing
  diagnostics at unchanged lines 533, 541, 1241, 1275, and 1283, with no
  diagnostics on the added test functions.
- Implementation commit: `5538e92` (full SHA recorded in the PR handoff).
- PR: PR #59 — `fix(edge): Issue #57 operator recovery 후 재작업 retry admission 수정`
  — `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/59` — OPEN.
- PR base/head: `main` / `fix/issue-57-operator-recovery-retry`.
- PR head SHA: `2e9dbc599e1e234eae196235f393ed795956d0b4`, verified through the live PR API.
- GitHub checks: none reported; hosted Actions remain disabled by repository
  policy.
- Merge performed: `NO`.
- Host deploy/live canary: `NOT RUN` (no host side effect requested).
- Human review/merge: pending downstream reviewer and human authority.

Rollback is a normal revert of this PR's four allowlisted files; no runtime
state migration or host rollback is introduced.
