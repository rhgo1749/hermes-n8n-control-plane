# REQ-090: 신선한 PR rework 라운드의 edge 소유권·delivery fail-closed

- Status: Rework round 4 implementation handoff
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `issue90/fresh-rework-ownership`
- Source-of-truth base: latest fetched `origin/main` (`dd7f6ad6c6fe39108d87a821c635046ab1fb88e1` at intake)
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-090-fresh-rework-ownership.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#90`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/90`
- Kanban task ID: `t_f469a6d5`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:90`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## 0. Mandatory repository route

Read `AGENTS.md`, `README.md`, `docs/README.md`, then the smallest canonical
lifecycle documents: `docs/EDGE_REWORK_LIFECYCLE.md`,
`docs/GITHUB_COMPLETION_LIFECYCLE.md`, and `docs/KANBAN_ROLE_CONTRACTS.md`.
Inspect `edge/kanban-github-sync.py` and
`edge/test-kanban-github-sync-rework.py` before editing.

## 1. Objective

Ensure a fresh trusted `agent-rework` request is owned and dispatched only by
the repository-owned edge rework lane. A default/core Kanban run, stale
same-head delivery, prior-round completion marker, or incomplete `DONE + OPEN
PR` state must never produce current-round delivery or
`agent-review-ready`.

## 2. Confirmed background and acceptance

- `_rework_request_comment_id()` must return `None` for label-only ingress;
  only an exact trusted `AGENT_REWORK_RETRY` acceptance may bind a comment id.
- Each fresh READY rework round remains visibly `agent-rework` until an
  edge-owned claim succeeds. The claim swaps it to `agent-working` and records
  run-linked claim/spawn provenance containing the round identity.
- `_task_run_after_rework()` accepts only the current round's trusted edge
  provenance. Ordinary/core runs may be used only for bounded crash/attention
  classification and can never satisfy delivery.
- A delivery requires the current-round edge run, trusted completion marker,
  exact task/request identity, matching live full head, and
  `validation=passed`; a durable delivery event is round-bound.
- The required same-head regression uses a production-valid
  `maintainer_retry` governing event (`repository`, `issue_number`, positive
  `pr_number`, full `head_sha`, `rework_round=2`, and matching
  `request_comment_id`/`retry_comment_id=42`) and the production call shape;
  a later same-head round-1 delivery must be rejected by the round comparison.
- `DONE + OPEN PR` without current-round delivery is repaired to `REVIEW`,
  records bounded `github_pr_rework_attention`/retry-visible state, and never
  projects `agent-review-ready`.
- Existing round-1 delivery, maintainer verification-only retry, active review
  lane, merge convergence, retry limits, and label claim-first behavior remain
  intact.
- Required regression names remain discoverable:
  - `test_fresh_rework_after_stale_review_ready_dispatches_round_two`
  - `test_done_open_without_current_round_delivery_never_projects_review_ready`
  - `test_task_run_after_rework_requires_edge_rework_provenance`
  - `test_label_only_rework_does_not_inherit_prior_completion_comment`
  - `test_prior_same_head_delivery_is_not_current_round_delivery`

### Control-plane ownership gate

- GitHub remains the durable PR/review surface.
- Hermes Kanban remains execution and claim authority.
- `edge/kanban-github-sync.py` remains the GitHub↔Kanban reconciliation owner.
- n8n remains trigger/glue only; no second dispatcher is introduced.
- H4V3 Overview and Telegram are not task-state owners.
- Ownership impact: `AFFECTED` within the existing edge owner.
- New state/store introduced: `NO`.
- Existing authority bypassed: `NO`.

### Security / policy gate

- Security impact: `AFFECTED` only at provenance/authorization boundaries.
- Auth/permission/secret boundary: `AFFECTED`; no credential is persisted,
  printed, or added to tracked files.
- Host/network exposure: `NONE`.
- External API/platform policy impact: `AFFECTED`; existing bounded GitHub
  client and trusted-actor policy remain authoritative.
- GitHub-hosted Actions stay disabled by repository policy.

## 3. In scope

1. Harden edge round reservation, shared-lock claim, run-linked provenance, and
   current-round delivery matching in `edge/kanban-github-sync.py`.
2. Preserve retry/crash recovery while separating strict delivery evidence from
   diagnostic-only ordinary-run classification.
3. Keep incomplete `DONE + OPEN PR` recovery fail-closed with visible
   `agent-rework` and bounded human-attention feedback.
4. Add the five named edge regressions and update positive fixtures to model
   production edge provenance.
5. Repair the false-positive same-head delivery regression fixture only; no
   production behavior change is expected from this round.
6. Maintain this shortened request as the repository-owned Issue #90 contract.

## 4. Explicit non-goals

- Hermes core source outside this repository changes.
- Modification, deletion, recreation, renaming, or rescheduling of the
  protected intake job `default:bf431b2a6ba6`.
- A second dispatcher, polling loop, n8n Schedule Trigger, new database/store,
  manual DB repair, arbitrary worker/PID handling, or worker killing.
- Changes to task schema, unrelated lifecycle/notification behavior, or
  unrelated cleanup.
- New PR creation for a rework, auto-merge, Issue closure, or GitHub Actions
  enablement.
- Treating a core/default-assignee run as edge delivery evidence.

## 5. Implementation requirements

- Fetch `origin` and work only on the dedicated branch/worktree.
- Use the existing Kanban DB/event tables and edge lock; do not modify Hermes
  core or introduce parallel state.
- Keep reservation and claim deterministic/idempotent; release/retry safely on
  projection or spawn failures, preserving `agent-rework` until claim success.
- Keep all marker, task, comment, round, head, and validation checks fail-closed.
- Keep GitHub label writes read-back verified and avoid sensitive logging.
- Use repository-relative paths in durable docs except the canonical local
  validation interpreter required by the repository contract.

## 6. Validation contract

Required local checks (GitHub Actions are `NOT RUN` by repository policy):

```bash
PYTHONDONTWRITEBYTECODE=1 env -u HERMES_DELEGATED_CHILD_CONTEXT \
  python3 edge/test-kanban-github-sync-rework.py
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile \
  edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py
ruff check edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py
git diff --check
```

Also run a focused command covering all five named regressions, inspect
available LSP/Pyright diagnostics for both touched Python files, and prove
regression tests fail under the pre-fix behavior (sabotage/equivalent isolated
comparison) before restoring and rerunning the fix. Keep local test, static,
remote CI, browser/host, and human merge evidence separate.

## 7. Remote delivery and stop state

Create or update exactly one Korean PR into `main`, with plain-text
`Closes #90.` outside code formatting. Read back title, base, head branch,
full SHA, changed-file set, and body through approved GitHub API paths; verify
`PullRequest.closingIssuesReferences` through GraphQL. Post one trusted
human-readable completion comment on that PR containing exactly:

```text
AGENT_REWORK_COMPLETE
task=t_f469a6d5
request_comment=5463162327
head=<full 40-character PR head SHA>
validation=passed
```

Do not merge, auto-merge, wait for future CI/review, or claim host/device
acceptance. Final handoff reports exact local results, PR identity, closing
reference evidence, `NOT RUN` gates, assumptions, residual risks, and the
`HUMAN_VALIDATION_REQUIRED` stop state.
