# REQ-032: rework head-binding 거부 PR 피드백 게이트

- Status: Implementation complete / Review pending
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT` / `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `fix/edge-head-advance-feedback-issue32`
- Source-of-truth base: `origin/main` at `f57e111d76e7d7dfa133f47e547fc27bd9889434` (fetched 2026-08-18)
- Remote delivery: Required; one PR against `main`
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-032-head-advance-feedback.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#32`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/32
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-main` (bounded direct implementation)
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## Objective

ctrl-hangul PR #74 round-13에서 구조적으로 올바른 completion marker가 round-requested head와 동일해 `rework_head_unchanged`로 거부됐지만 PR 피드백이 없어 에이전트가 같은 head를 반복 제출했다. `run_head_mismatch`도 같은 head-binding 계열의 무음 retry였다.

- 두 reason에 대해 기존 fail-closed retry/state routing은 유지한다.
- 거부 시 PR에 idempotent `HERMES_KANBAN_REWORK_ATTENTION task= reason=` 피드백을 게시한다.
- 코멘트는 requested head, 반드시 새 worker-owned head로 전진해야 한다는 요구, exact completion template를 포함한다.
- 문서/스킬 체크리스트에 `head != requested_head`와 run summary/metadata head binding을 명시한다.

## Confirmed background / root cause

- `_rework_delivery_evidence`는 requested head와 marker head가 같으면 `rework_head_unchanged`, run metadata/summary head와 marker head가 다르면 `run_head_mismatch`를 반환한다.
- 기존 `_rework_human_attention`에는 이 두 reason이 없어 retry path가 조용히 재시도했다.
- PR #74 round-13: worker가 `df81c1b` same-head marker를 게시했고, lead가 `8fa6485`를 별도 push했으나 worker run이 그 head를 기록하지 않아 delivery event가 생기지 않았다.
- Ownership: GitHub PR is review surface; Kanban is execution state; edge is reconciliation owner. No new store/dispatcher/state.

## In scope

1. `edge/kanban-github-sync.py`: head-binding reason set, `_post_rework_attention_pr_comment(... requested_head=...)`, retry path gated feedback for `rework_head_unchanged` and `run_head_mismatch`; no auto-retry/state behavior change.
2. `edge/test-kanban-github-sync-rework.py`: tests 106–107 for same-head and run-head mismatch feedback, idempotency and requested-head guidance.
3. `docs/EDGE_REWORK_LIFECYCLE.md`: head must differ from requested head; worker run must record the same head; feedback reason matrix.
4. `references/rework-completion-marker-format.md` in kanban-main skill: worker checklist update.

## Explicit non-goals

- Hermes core modification, automatic retry/round creation, label policy change.
- Review-card delivery evaluation redesign or review↔running loop policy (separate follow-up; fail-closed contract must be independently reviewed).
- Direct mutation of ctrl-hangul PR #74 beyond the already-published operator feedback.
- GitHub Actions changes.

## Validation contract

- `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py` — **PASS: 596 passed, 0 failed** (tests 106–107 included).
- `python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py` — **PASS**.
- `git diff --check` — required before delivery.
- Pyright baseline comparison — required; no new diagnostics expected.

## Delivery / host deployment

- After PR merge, deploy the Git-tracked canonical copies with:
  `automation/hermes/scripts/deploy-intake-edge.sh --hermes-home /home/hermes/.hermes`
- Verify installed script SHA-256 equals merged `main`; cron job remains untouched.
- Rollback uses the timestamped `.bak-*` paths printed by the deploy script.

## Completion

- Source Issue / REQ provenance recorded.
- Fail-closed state/retry behavior preserved.
- PR created, reviewed, merged only after user authority.
- Live installed bytes match merged main after deployment.
