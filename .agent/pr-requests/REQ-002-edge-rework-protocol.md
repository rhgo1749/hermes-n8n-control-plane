# REQ-002: 검증된 BLOCKED rework delivery의 REVIEW projection 보정

- Status: Rework implemented / Review pending
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT` / `N8N_VALIDATE` / `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `fix/edge-pr-rework-protocol-issue2`
- Source-of-truth base: `origin/main` at `e1e0119a7ebe4439ff27bd71e01525076e50d174` (fetched 2026-08-16; after PR #25 merge `origin/main` = `9a3fd5fa281324d43ab30a76125ce0201f1603ed`)
- Remote delivery: Required; NEW PR against `main` (predecessor PR #25 was merged by the human before this rework and is final)
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-002-edge-rework-protocol.md`
- Merge authority: Human/user only; no merge or auto-merge performed
- Source issue: `rhgo1749/H4V3-Meowcore#2`
- Source issue URL: `https://github.com/rhgo1749/H4V3-Meowcore/issues/2`
- Kanban task ID: `t_c6b8e615` (rework round 2; reworks `t_a7cd1b9e` / `t_df2447e4`)
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

현재 rework round의 worker가 원격 PR head를 갱신하고 검증을 통과했지만 human/device validation 때문에 Kanban run outcome이 `blocked`가 되는 경우, 강한 current-round delivery evidence가 있으면 GitHub `agent-review-ready`와 Kanban `REVIEW`를 투영한다. 반대로 marker/head/validation/run evidence가 없거나 틀리면 generic blocked reconciliation으로 누수하지 않고 BLOCKED/needs input attention에 fail-closed 한다. consumed round의 provenance가 해체된 경우(이벤트 `pr_number`가 canonical PR로 해석되지 않음 — 존재하지 않는 PR, wrong task, recreated/mismatched PR, ambiguous decision)에도 round ownership을 보존하고 같은 fail-closed attention 경로로 라우팅한다. human merge는 계속 별도 gate로 유지한다.

## Confirmed background and root cause

- 기존 보정은 consumed `github_pr_rework` context에서 invalid/incomplete blocked delivery가 `_reconcile_rework_lifecycle()`의 `None` 반환 후 일반 `_reconcile_blocked()`로 fall-through했다.
- 그 결과 open non-draft PR이 `REVIEW`로 투영되고 `agent-working`이 남을 수 있었다. 이는 `github_pr_sync`를 잘못 생성하는 fail-closed 위반이다.
- `AGENT_REWORK_COMPLETE` marker의 소유자는 worker가 읽는 edge-owned task comment contract이며, edge는 trusted PR comment의 task/request-comment/head/validation과 종료 run evidence를 함께 검증한다.
- canonical provenance는 현재 Kanban task의 최신 `github_pr_rework` event와 request-comment identity다. PR body의 과거 task id 또는 recreated task는 current round evidence가 아니다.
- Rework round 2 (review `t_d29773d4`): `_rework_context()`가 consumed 이벤트의 `pr_number`가 정확히 하나의 canonical PR로 해석되지 않을 때 `None`을 반환하면, BLOCKED 카드가 round ownership을 잃고 일반 `_reconcile_blocked()`에 도달해 open PR을 `linked_pr_open`으로 REVIEW 투영하는 fail-open이 재현됐다 (nonexistent `pr_number=999` fixture).
- Rework round 2 (review `t_d29773d4`): 이 REQ 문서의 "Final source/local/pushed SHA"가 `daaa73b...`로 기록되었으나 실제 local HEAD / `origin/fix/edge-pr-rework-protocol-issue2` / `refs/pull/25/head`는 모두 `db8838e21847b7aeb92e5edea23233338d264876`였고, PR #25 상태가 OPEN으로 기록되어 있으나 이후 human이 merge했다.

## In scope

1. `edge/kanban-github-sync.py`에서 consumed rework의 invalid/incomplete BLOCKED evidence가 generic blocked projection으로 fall-through하지 않도록 attention/retry 경로로 고정.
2. consumed round의 provenance mismatch(`pr_number` 해석 실패/ambiguous)가 BLOCKED task의 round ownership을 보존하고, REVIEW projection/`github_pr_sync`/`github_pr_rework_delivery`/label mutation 없이 idempotent attention으로 fail-closed 하도록 `sync_board` blocked flow guard와 `_rework_provenance_attention` helper 추가.
3. `edge/test-kanban-github-sync-rework.py`에 finished clean rc=0/no marker, wrong full head, validation!=passed blocked regressions 및 second-tick idempotency, wrong-task completion marker (test 80), unresolvable `pr_number=999` consumed round (test 81), mismatched/recreated PR consumed round (test 82) 추가.
4. 강한 current-round marker/task/request-comment/full-live-head/trusted actor-time/validation=passed/bounded finished-run evidence만 REVIEW/review-ready로 허용.

## Explicit non-goals

- Hermes core (`kanban_db.py`, core tools, CLI, worker protocol) 수정
- H4V3-Meowcore source 또는 외부 PR #3 수정/merge/label one-off mutation
- changed-head 기반 weak recovery, second dispatcher/state store, GitHub Actions 변경
- human merge/device acceptance 자동화
- 이미 merged된 control-plane PR #25 수정 (merged 상태는 final)

## Validation contract and evidence

- `EDGE_REWORK`: `/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py` — **476 passed, 0 failed** (rework round 1: 439; +37 new checks across tests 80/81/82)
- Regression coverage: blocked clean/no-marker, wrong-head, validation-not-passed each remain BLOCKED, avoid REVIEW/`github_pr_sync`/`github_pr_rework_delivery`, and do not duplicate attention on second tick — PASS; wrong-task marker and mismatched/unresolvable consumed-round provenance stay BLOCKED with `agent-working` retained — PASS (pre-fix reproduction of tests 81/82: task flipped to `review` via `linked_pr_open` + `github_pr_sync`, confirmed by running them against the pre-fix `sync_board`)
- Python compile: `python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py automation/hermes/scripts/github-agent-ready-kanban-intake.py` — PASS
- `python3 tests/test_repo_scoped_intake.py` — 15 tests PASS
- `python3 tests/test_h4v3_overview.py` — 12 tests PASS
- `python3 tests/test_h4v3_notification_policy.py` — 8 tests PASS
- `/ws/hermes-agent/venv/bin/python3 tests/test_github_event_concurrency_contract.py` — PASS
- `/ws/hermes-agent/venv/bin/python3 automation/n8n/scripts/validate.py` — PASS (`ok=true`)
- `bash -n automation/hermes/scripts/deploy-intake-edge.sh` and `git diff --check origin/main...HEAD` / `git diff --check HEAD` — PASS
- Available pyright: `/home/hermes/.hermes/profiles/kanban-main/lsp/node_modules/.bin/pyright` with `PYTHONPATH=/ws/hermes-agent` over `edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py` — candidate 7 diagnostics, `origin/main` baseline 7 diagnostics, normalized (file/line/rule/severity) sets identical, **0 candidate-only** — PASS (the 7 are pre-existing test-harness typing issues, unchanged by this rework)
- Official deploy/read-back — **NOT RUN**; deployment is a separate post-approval gate. Installed `/home/hermes/.hermes/scripts/kanban-github-sync.py` SHA-256 `0f8ee408669fbcd971ad179262892055fefd0b278e9768e77dc32980a5d50721` differs from candidate tracked `edge/kanban-github-sync.py` SHA-256 (candidate at final push: recorded in handoff) — the installed copy predates this rework
- Authenticated live GitHub reconciliation canary — **NOT RUN** (post-approval gate; no live reconciliation performed in this round)

## Delivery / stop state

- Reviewed predecessor: `42ce8088afb17869a3aba3b43e97308967e15f75` (round 1 implementation)
- Round 1 commits: implementation `79b23fd344dd0f7ee43de9035b1a7699292c17a1`, provenance `daaa73b6f671dd89a37c9fd2fa0a0f6847c7adc6`, pushed provenance `db8838e21847b7aeb92e5edea23233338d264876`
- **Predecessor PR #25** (`https://github.com/rhgo1749/hermes-n8n-control-plane/pull/25`): **MERGED by human (rhgo1749, GitHub UI)** — merge commit `9a3fd5fa281324d43ab30a76125ce0201f1603ed`, mergedAt `2026-08-16T03:20:46Z`, head `db8838e21847b7aeb92e5edea23233338d264876`. Merged state is final; this rework does not modify it.
- **Rework round 2 implementation commit**: `622d679ab2abb672ee97f4f1de52262ec5557636` — fail-closed provenance guard + tests 80/81/82
- **Rework round 2 provenance chain**: implementation `622d679ab2abb672ee97f4f1de52262ec5557636` → provenance refresh `d0ce2204871d2cce681704f69ba53678765c43a7` → this PR-identity note (the chain's final commit; a commit cannot record its own SHA, so this note records the preceding provenance commit and the read-backs below, and the final branch/PR head OID observed after the last push is recorded in the `t_c6b8e615` Kanban handoff metadata)
- **New PR #27** (`https://github.com/rhgo1749/hermes-n8n-control-plane/pull/27`): base `main`, head `fix/edge-pr-rework-protocol-issue2`. Authenticated read-back (2026-08-16, `gh` as rhgo1749) taken when the branch head was `d0ce2204871d2cce681704f69ba53678765c43a7`: state **OPEN** (not draft), base `main`, head `fix/edge-pr-rework-protocol-issue2`, head OID `d0ce2204871d2cce681704f69ba53678765c43a7`, `MERGEABLE`, changed files = exactly the three scoped files (`edge/kanban-github-sync.py` +97, `edge/test-kanban-github-sync-rework.py` +186, `.agent/pr-requests/REQ-002-edge-rework-protocol.md` +23/-18). After the final push of this chain, `local HEAD == origin/fix/edge-pr-rework-protocol-issue2 == PR #27 head OID` was re-verified (value in the `t_c6b8e615` handoff). No merge/auto-merge performed; merge authority remains human/user.
- Expected changed files remain exactly: `edge/kanban-github-sync.py`, `edge/test-kanban-github-sync-rework.py`, this REQ document.

## Rollback

Use the timestamped backup paths printed by `automation/hermes/scripts/deploy-intake-edge.sh` only if a later authorized deployment occurs; restore deployed files with `mv` after confirming the intended backup. Git rollback is the commit-level alternative (revert the round-2 implementation commit on the branch before merge); do not touch cron state.
