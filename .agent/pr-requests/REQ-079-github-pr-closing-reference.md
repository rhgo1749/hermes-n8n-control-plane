# REQ-079 · GitHub PR closing-reference 작성·검증 가드

- Source Issue: #79 (https://github.com/rhgo1749/hermes-n8n-control-plane/issues/79)
- Source repository: `rhgo1749/hermes-n8n-control-plane`
- Base: `main` at `57957ca3e39ea0c6475f457ad8450d7721b1b42d`
- Kanban root task: `t_2935f597` (intake idempotency key `github:rhgo1749/hermes-n8n-control-plane:issue:79`)
- Implementation task: `t_a7369eda`; delivery branch: `wt/t_a7369eda`
- Delivery: one Korean HOTFIX PR against `main`; merge/auto-merge is human-only.

## Problem

GitHub closes an Issue only when a merged PR body carries a closing keyword
(Closes/Fixes/Resolves) adjacent to the Issue number as *visible plain text*,
and that relationship is reflected in GraphQL
`PullRequest.closingIssuesReferences`. ctrl-hangul#70 stayed open after
merged PR #82 because its `Closes #70` line was inside backticks and the
word-adjacent mention `Issue #70의` was never a closing reference. The
GitHub-backed intake contract must prevent this class of mistake in every
generated developer/lead handoff.

## Required change

1. Canonical task-body contract
   (`automation/hermes/scripts/github-agent-ready-kanban-intake.py`): add a
   plain-text `GitHub PR closing-reference contract (pre-handoff)` section
   with the `Closes #<issue-number>.` shape, the outside-backticks/fences +
   whitespace/punctuation-termination rule, the explicit
   word-adjacent-mention exclusion (`Issue #N의`), the existing-PR-only
   REST fresh-read → GraphQL `closingIssuesReferences` verification → SAME-PR
   REST JSON PATCH → read-back flow, the forbidden operations (second/new PR,
   merge, auto-merge, post-merge Issue close, local-comment inference), and
   the fail-closed outcomes (identity ambiguity, GraphQL/REST/PATCH failure,
   failed read-back). The rendered body also states the card's own Issue
   number line (`Closes #79.` for this card).
2. Deployed overlay
   (`automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py`):
   require the canonical core's exact shared contract section before emitting
   the live body and keep the drift guard fail-closed. The wording has one
   source of truth in the canonical implementation; a focused regression
   verifies that the overlay emits it and rejects drift.
3. Deterministic helper `body_has_valid_closing_reference` +
   `_closing_visible_lines` (pure, no network) model the authoring syntax only;
   GitHub GraphQL remains the relationship authority. Fixture tests cover
   plain-text acceptance and backticked/fenced, word-adjacent, and extra-digit
   rejection.

## Non-goals

- No change to the `merged_at != null` GitHub completion authority, the
  `closingIssuesReferences` intake guard semantics (commit `48a70aa`), edge/
  Kanban ownership, Hermes core, n8n glue, or any polling/Schedule Trigger.
- No retroactive correction of live Issues/PRs, no second PR, no merge or
  auto-merge, no post-merge Issue close.
- No GitHub Actions (repository policy: disabled).

## Validation

- `python3 tests/test_intake_completion_contract_entrypoint.py` (extended with
  the closing-reference regressions; bite proof: the new assertions fail on
  the pre-fix contract and pass after restoration)
- `python3 tests/test_intake_merged_pr_guard.py`
- `python3 tests/test_repo_scoped_intake.py`
- `PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile` on both scripts + changed test
- exact-file Pyright (profile LSP) diagnostics, new-vs-baseline comparison
- `bash -n` + `deploy-intake-edge.sh --dry-run` (candidate validated; no write)
- `git diff --check` + secret/placeholder hygiene
- live PR REST fresh-read + GraphQL `closingIssuesReferences` read-back
  (Issue #79 required in the relation; if absent, SAME-PR REST JSON PATCH +
  double read-back; never `gh pr edit`)

## Pre-delivery evidence

- Focused contract, merged-PR guard, and repository-scope tests: PASS.
- Bite proof: the new rendered-body assertion failed against fetched
  `origin/main` with `AssertionError`; the restored candidate test passes.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile` on both intake scripts and
  the changed test: PASS; `bash -n automation/hermes/scripts/deploy-intake-edge.sh`:
  PASS; isolated `deploy-intake-edge.sh --dry-run`: PASS.
- Profile `basedpyright` exact-file comparison: base 80 errors / 973 warnings;
  candidate 80 errors / 1016 warnings; candidate-only errors: 0. The 43
  candidate-only warnings are dynamic test-harness `ModuleType`/`Any` warnings;
  no new type or missing-import error was introduced.
- Broader repository deterministic gate: 18 PASS; three unrelated baseline
  failures remain (`test_n8n_cron_auth_plugin.py` is absent, cron-trigger
  pause assertion fails, and cutover fixture lacks `state-root.sh`).

## Stop state

`HUMAN_VALIDATION_REQUIRED` + post-merge host deployment NOT RUN: live
runtime hash/contract-marker read-back requires the exact merged
`origin/main` and explicit human merge authorization. Minimum operator
command (post-merge):
`bash automation/hermes/scripts/deploy-intake-edge.sh --hermes-home /home/hermes/.hermes`
