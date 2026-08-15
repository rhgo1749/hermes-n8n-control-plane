# REQ-002: 검증된 BLOCKED rework delivery의 REVIEW projection 보정

- Status: Rework implemented / Review pending
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT` / `N8N_VALIDATE` / `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `fix/edge-pr-rework-protocol-issue2`
- Source-of-truth base: `origin/main` at `e1e0119a7ebe4439ff27bd71e01525076e50d174`
- Remote delivery: Required; existing control-plane PR #25 only
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-002-edge-rework-protocol.md`
- Merge authority: Human/user only; no merge or auto-merge performed
- Source issue: `rhgo1749/H4V3-Meowcore#2`
- Source issue URL: `https://github.com/rhgo1749/H4V3-Meowcore/issues/2`
- Kanban task ID: `t_a7cd1b9e` (rework of `t_df2447e4`)
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

현재 rework round의 worker가 원격 PR head를 갱신하고 검증을 통과했지만 human/device validation 때문에 Kanban run outcome이 `blocked`가 되는 경우, 강한 current-round delivery evidence가 있으면 GitHub `agent-review-ready`와 Kanban `REVIEW`를 투영한다. 반대로 marker/head/validation/run evidence가 없거나 틀리면 generic blocked reconciliation으로 누수하지 않고 BLOCKED/needs input attention에 fail-closed 한다. human merge는 계속 별도 gate로 유지한다.

## Confirmed background and root cause

- 기존 보정은 consumed `github_pr_rework` context에서 invalid/incomplete blocked delivery가 `_reconcile_rework_lifecycle()`의 `None` 반환 후 일반 `_reconcile_blocked()`로 fall-through했다.
- 그 결과 open non-draft PR이 `REVIEW`로 투영되고 `agent-working`이 남을 수 있었다. 이는 `github_pr_sync`를 잘못 생성하는 fail-closed 위반이다.
- `AGENT_REWORK_COMPLETE` marker의 소유자는 worker가 읽는 edge-owned task comment contract이며, edge는 trusted PR comment의 task/request-comment/head/validation과 종료 run evidence를 함께 검증한다.
- canonical provenance는 현재 Kanban task의 최신 `github_pr_rework` event와 request-comment identity다. PR body의 과거 task id 또는 recreated task는 current round evidence가 아니다.

## In scope

1. `edge/kanban-github-sync.py`에서 consumed rework의 invalid/incomplete BLOCKED evidence가 generic blocked projection으로 fall-through하지 않도록 attention/retry 경로로 고정.
2. `edge/test-kanban-github-sync-rework.py`에 finished clean rc=0/no marker, wrong full head, validation!=passed blocked regressions 및 second-tick idempotency 추가.
3. 강한 current-round marker/task/request-comment/full-live-head/trusted actor-time/validation=passed/bounded finished-run evidence만 REVIEW/review-ready로 허용.

## Explicit non-goals

- Hermes core (`kanban_db.py`, core tools, CLI, worker protocol) 수정
- H4V3-Meowcore source 또는 외부 PR #3 수정/merge/label one-off mutation
- changed-head 기반 weak recovery, second dispatcher/state store, GitHub Actions 변경
- human merge/device acceptance 자동화

## Validation contract and evidence

- `EDGE_REWORK`: `/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py` — **439 passed, 0 failed**
- Regression coverage: blocked clean/no-marker, wrong-head, validation-not-passed each remain BLOCKED, avoid REVIEW/`github_pr_sync`/`github_pr_rework_delivery`, and do not duplicate attention on second tick — PASS
- Python compile: `python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py automation/hermes/scripts/github-agent-ready-kanban-intake.py` — PASS
- `python3 tests/test_repo_scoped_intake.py` — 15 tests PASS
- `python3 tests/test_h4v3_overview.py` — 12 tests PASS
- `python3 tests/test_h4v3_notification_policy.py` — 8 tests PASS
- `/ws/hermes-agent/venv/bin/python3 tests/test_github_event_concurrency_contract.py` — PASS
- `/ws/hermes-agent/venv/bin/python3 automation/n8n/scripts/validate.py` — PASS
- `bash -n automation/hermes/scripts/deploy-intake-edge.sh` and `git diff --check` — PASS
- Available pyright: `/home/hermes/.hermes/profiles/kanban-main/lsp/node_modules/.bin/pyright` with `PYTHONPATH=/ws/hermes-agent`; candidate and `origin/main` baseline both report 7 diagnostics with no candidate-only diagnostic — PASS (baseline-equivalent; diagnostics are pre-existing test-harness issues)
- Official deploy/read-back — NOT RUN; this rework was not deployed from this worker
- Authenticated live GitHub reconciliation canary — NOT RUN: `GITHUB_TOKEN`/`GH_TOKEN` unavailable; public PR API read-back also unavailable (HTTP 404 for private repository)

## Delivery / stop state

- Reviewed predecessor: `42ce8088afb17869a3aba3b43e97308967e15f75`
- Rework implementation commit: `79b23fd344dd0f7ee43de9035b1a7699292c17a1`
- Final source/local/pushed SHA: `daaa73b6f671dd89a37c9fd2fa0a0f6847c7adc6` (the pushed implementation + provenance commit; this follow-up records its verified identity)
- Existing PR #25: `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/25`; requested state is OPEN against `main`, no merge/auto-merge. Live authenticated state/base/head/body/files read-back is unavailable in this environment because `gh` is not installed and unauthenticated private API returned HTTP 404.
- Expected changed files remain exactly: `edge/kanban-github-sync.py`, `edge/test-kanban-github-sync-rework.py`, this REQ document.

## Rollback

Use the timestamped backup paths printed by `automation/hermes/scripts/deploy-intake-edge.sh` only if a later authorized deployment occurs; restore deployed files with `mv` after confirming the intended backup. Git rollback is the commit-level alternative; do not touch cron state.
