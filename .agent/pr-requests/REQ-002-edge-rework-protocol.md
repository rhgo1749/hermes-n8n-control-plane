# REQ-002: 검증된 BLOCKED rework delivery의 REVIEW projection 보정

- Status: Implemented / Review pending
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT` / `N8N_VALIDATE` / `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `fix/edge-pr-rework-protocol-issue2`
- Source-of-truth base: freshly fetched `origin/main`
- Remote delivery: Required
- Pull request title/body/final report language: Korean-compatible evidence; concise technical report
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-002-edge-rework-protocol.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/H4V3-Meowcore#2`
- Source issue URL: `https://github.com/rhgo1749/H4V3-Meowcore/issues/2`
- Kanban task ID: `t_df2447e4`
- Intake idempotency key: external H4V3-Meowcore task provenance; no local intake key
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

현재 rework round의 worker가 원격 PR head를 갱신하고 검증을 통과했지만 human/device validation 때문에 Kanban run outcome이 `blocked`가 되는 경우, 강한 current-round delivery evidence가 있으면 GitHub `agent-review-ready`와 Kanban `REVIEW`를 투영한다. human merge는 계속 별도 gate로 유지한다.

## Confirmed background and root cause

- `AGENT_REWORK_COMPLETE` marker의 소유자는 worker가 읽는 edge-owned task comment contract이며, edge는 trusted PR comment의 task/request-comment/head/validation과 종료 run evidence를 함께 검증한다.
- 기존 lifecycle은 `blocked` 상태를 lifecycle reconciliation 전에 일반 blocked reconciliation으로 보내고, worker-owned lifecycle 함수도 `blocked`를 무조건 건너뛰었다. 따라서 validated blocked delivery가 review-ready projection을 잃을 수 있었다.
- clean `rc=0` protocol omission / self-termination 사례는 marker 없는 종료로 취급해야 하며, changed head만으로 복구하지 않는다.
- canonical provenance는 현재 Kanban task의 최신 `github_pr_rework` event와 그 request-comment identity다. PR body의 과거 task id 또는 다른 recreated task는 current round evidence가 아니다.

## In scope

1. `edge/kanban-github-sync.py`에서 blocked task도 current-round complete delivery evidence가 있을 때만 REVIEW/review-ready로 투영.
2. blocked lifecycle reconciliation을 일반 blocked projection보다 먼저 평가하되, evidence가 없으면 기존 fail-closed blocked path를 유지.
3. `edge/test-kanban-github-sync-rework.py`에 blocked human-validation delivery regression 추가 및 전체 A-H lifecycle matrix 유지.

## Explicit non-goals

- Hermes core (`kanban_db.py`, core tools, CLI, worker protocol) 수정
- H4V3-Meowcore source 또는 외부 PR #3 수정/merge/label one-off mutation
- changed-head 기반 weak recovery, second dispatcher/state store, GitHub Actions 변경
- human merge/device acceptance 자동화

## Validation contract and evidence

- `EDGE_REWORK`: `/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py` — 418 passed, 0 failed
- Python compile: edge/intake/test changed Python files — PASS
- `python3 tests/test_repo_scoped_intake.py` — 15 tests PASS
- `python3 tests/test_h4v3_overview.py` — 12 tests PASS
- `python3 tests/test_h4v3_notification_policy.py` — 8 tests PASS
- `/ws/hermes-agent/venv/bin/python3 tests/test_github_event_concurrency_contract.py` — PASS
- `/ws/hermes-agent/venv/bin/python3 automation/n8n/scripts/validate.py` — PASS
- `git diff --check` and shell syntax — PASS
- Official deploy script dry-run and real deploy — PASS; live deployed bytes match tracked sources by SHA-256; cron was reported untouched.
- Live reconciliation canary — NOT RUN: deployed script reports `GITHUB_TOKEN/GH_TOKEN is unavailable` in the authorized runtime.
- LSP/type diagnostics — NOT RUN: no `pyright`/`basedpyright` executable is available.

## Delivery / stop state

- Candidate commit: `30e6e5660a58e5739d8b3a641a1cdef17fa83a60`
- Remote branch SHA verified equal to local SHA.
- Control-plane PR #25 is OPEN against `main`; no merge/auto-merge performed.
- Human review and live GitHub canary remain outstanding.

## Rollback

Use the timestamped backup paths printed by `automation/hermes/scripts/deploy-intake-edge.sh`; restore the three deployed files with `mv` after confirming the intended backup. Git rollback is the commit-level alternative; do not touch cron state.
