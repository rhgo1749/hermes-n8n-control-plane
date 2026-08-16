# REQ-002b: rework_human_attention 이후 명시적 retry로 새 rework round 재시작 (AGENT_REWORK_RETRY)

- Status: Rework implemented / Review pending
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT` / `N8N_VALIDATE` / `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `fix/edge-rework-explicit-retry-issue2`
- Source-of-truth base: `origin/main` at `9a3fd5f` (PR #25 merge)
- Remote delivery: Required; new control-plane PR (previous PR #25 already merged)
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-002b-edge-rework-explicit-retry.md`
- Merge authority: Human/user only; no merge or auto-merge performed
- Source issue: `rhgo1749/H4V3-Meowcore#2` (external reproduction: H4V3-Meowcore PR #3)
- Source issue URL: `https://github.com/rhgo1749/H4V3-Meowcore/issues/2`
- Kanban task ID: source-task 기준 edge lifecycle (H4V3-Meowcore PR #3 연결 task)
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer` (본 PR은 lead가 직접 bounded 구현)
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

`rework_human_attention` hold(BLOCKED + self-heal로 복구된 `agent-rework` label)에서
사람이 명시적으로 요청할 때만 새 rework round를 시작할 수 있는 ingress를 추가한다.
fail-closed(자동 retry 금지)는 유지하고, 신뢰된 maintainer의 machine-readable
`AGENT_REWORK_RETRY` PR comment만이 새 `github_pr_rework` round를 연다.

## Confirmed background and root cause

- main loop에서 `status == "blocked"`이고 consumed rework event가 있으면
  `_reconcile_rework_lifecycle()`이 `_reconcile_blocked()`보다 먼저 실행된다
  (edge/kanban-github-sync.py, blocked 분기).
- lifecycle blocked 분기는 delivery evidence만 검사하고 불완전하면
  `rework_human_attention`을 기록한 뒤 항상 entry를 반환하므로
  `continue`로 classic `_reconcile_blocked()`(Precedence 2: fresh label →
  `evaluate_rework` → `apply_rework` → 새 `github_pr_rework` event → READY)에
  도달할 수 없다. old `github_pr_rework` event가 lifecycle_context를 계속 지배한다.
- `agent-rework` label은 edge self-heal(`_restore_rework_labels`)이 직접 복구하므로
  label 존재/갱신만으로는 human retry evidence가 될 수 없다
  (label 재부착 시점 > round event 시점이면 classic 경로가 자동 READY로 누수할 위험).
- 따라서 BLOCKED + attention hold에서 사람의 명시적 retry가 없으면 영원히
  BLOCKED에 머무는 fail-closed는 의도대로지만, 새 round로 나갈 수 있는
  explicit ingress가 없었다 (이번 패치 대상).

## Explicit retry contract (선택 이유)

기존 repository에 rework retry용 trusted comment/review/label-event contract가
없음을 확인: `agent-rework` label은 edge self-heal과 충돌해 신호로 쓸 수 없고,
`evaluate_blocked_resume`(Issue comment 기반)은 no-PR 카드 전용이며 lifecycle
intercept 때문에 PR-backed rework 카드에는 도달하지 못한다. `AGENT_REWORK_COMPLETE`
marker는 completion 전용이다.

그래서 가장 작고 machine-readable하며 stale-safe한 신호를 신규 도입한다:

```text
AGENT_REWORK_RETRY
task=<task_id>
```

- 현재 PR의 comment로 `TRUSTED_GITHUB_ACTORS` maintainer가 작성
- line-exact marker + `task=` 정확 바인딩 (fuzzy 자연어 매칭 없음)
- 마지막 `github_pr_rework_attention`(및 governing event)보다 strictly 이후
- Issue OPEN + `agent-ready` 재검증
- 한 번만 소비: 새 round의 `github_pr_rework` payload에
  `trigger: "maintainer_retry"` + `retry_comment_id`를 기록 → 동일 comment id는
  영구히 재사용 불가 (다음 round에서 stale retry 재사용 금지)
- 소비 시 `apply_rework` 경유로 정확히 하나의 새 `github_pr_rework` event 생성,
  BLOCKED → READY, `request_comment`는 retry comment id로 바인딩되어
  새 round의 completion marker가 이를 참조해야 함
- dry-run은 `maintainer_retry_predicted`로 예측만, mutation 없음
- GitHub 조회 실패/Issue closed/agent-ready 없음/untrusted/malformed/pre-attention/
  already-consumed → 전부 fail-closed (BLOCKED 유지)

## In scope

1. `edge/kanban-github-sync.py`: `REWORK_RETRY_MARKER` 상수,
   `_last_rework_attention_at` / `_consumed_retry_comment_ids` /
   `_find_rework_retry_signal` / `_consume_explicit_rework_retry` 헬퍼,
   `apply_rework(retry_comment_id=...)` 확장(payload에 trigger/retry_comment_id),
   `_reconcile_rework_lifecycle` blocked 분기에 retry 검사 선행,
   `_record_rework_attention` comment에 retry 지침 추가,
   소비 시 `consecutive_failures` 리셋(사람 명시 retry = fresh attempt).
2. `edge/test-kanban-github-sync-rework.py`: 테스트 80–94 추가
   (regression 1–14 전부 + dry-run + one-shot + Issue 조건).
3. `docs/EDGE_REWORK_LIFECYCLE.md`: explicit maintainer retry 섹션/테이블/검증 수치.
4. 이 REQ 문서.

## Explicit non-goals

- Hermes core (`kanban_db.py`, core tools, CLI, worker protocol) 수정
- H4V3-Meowcore source 또는 외부 PR #3 수정/merge/label one-off mutation
- `if BLOCKED and agent-rework: READY` 류 자동 승격, head/rc=0 기반 weak recovery
- old completion marker 재사용, attention 직후/타이머 기반 자동 retry
- GitHub-hosted Actions 변경, merge/auto-merge
- PR #3 acceptance를 label 수동 편집으로 꾸미기

## Validation contract and evidence

- `EDGE_REWORK`: `/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py`
  — **517 passed, 0 failed** (기존 439 + 신규 78)
- Python compile: `python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py` — PASS
- `python3 tests/test_repo_scoped_intake.py` — PASS (15)
- `python3 tests/test_h4v3_overview.py` — PASS (12)
- `python3 tests/test_h4v3_notification_policy.py` — PASS (8)
- `/ws/hermes-agent/venv/bin/python3 tests/test_github_event_concurrency_contract.py` — PASS
- `/ws/hermes-agent/venv/bin/python3 automation/n8n/scripts/validate.py` — PASS
- `bash -n automation/hermes/scripts/deploy-intake-edge.sh` + `git diff --check` — PASS
- pyright: `/home/hermes/.hermes/profiles/kanban-main/lsp/node_modules/.bin/pyright`
  with `PYTHONPATH=/ws/hermes-agent`; candidate vs `origin/main` baseline
  동일 진단 집합 (candidate-only diagnostic 없음) — PASS
- Official deploy/read-back — NOT RUN (이번 worker는 deploy하지 않음; 준비 상태와
  rollback 절차만 기록)
- Authenticated live GitHub reconciliation canary (H4V3-Meowcore PR #3) — NOT RUN:
  `GITHUB_TOKEN`/`GH_TOKEN` 없음, private repo unauthenticated API 404

## Delivery / stop state

- Implementation commit: `c4778d9c34c8ed59317867435444f3630acc1bc9`
- Local/pushed SHA: `c4778d9c34c8ed59317867435444f3630acc1bc9`
  (`origin/fix/edge-rework-explicit-retry-issue2`와 동일)
- PR: https://github.com/rhgo1749/hermes-n8n-control-plane/pull/28 — OPEN 유지,
  merge/auto-merge 금지 (mergeable: MERGEABLE)
- Expected changed files: `edge/kanban-github-sync.py`,
  `edge/test-kanban-github-sync-rework.py`, `docs/EDGE_REWORK_LIFECYCLE.md`,
  `.agent/pr-requests/REQ-002b-edge-rework-explicit-retry.md`
- Live deploy (2026-08-16): `automation/hermes/scripts/deploy-intake-edge.sh
  --hermes-home /home/hermes/.hermes` 실행 완료 —
  live `/home/hermes/.hermes/scripts/kanban-github-sync.py` SHA-256
  `b6e156a57c833dc064c5315c700da8f0b39410a0266c73d983d00935b4cf53d7` ==
  checkout `edge/kanban-github-sync.py` (intake/registry는 기존 live와 바이트
  동일, edge만 변경). cron job `bf431b2a6ba6` id/schedule/enabled/script 무변경.
  rollback backup: `/home/hermes/.hermes/scripts/.bak-kanban-github-sync.py-20260816T053858Z`
- H4V3-Meowcore PR #3 acceptance (live read-only canary):
  배포 후 첫 cron tick(2026-08-16T14:40+09:00, job `bf431b2a6ba6`, completed
  @14:41:13)에서 `t_4093eec7`(h4v3-meowcore board)은
  `reason: rework_human_attention`, `diagnostic: completion_handoff_missing`,
  `lifecycle.labels: [agent-rework]`, `label_action: labels_unchanged`로
  **BLOCKED 유지 확인** — `maintainer_retry_consumed`/새 `github_pr_rework`
  event/READY 없음. PR #3 labels `[agent-rework]` 불변. 동일 틱에서
  ctrlhangul #71의 attention hold도 보존 확인 (live regression). maintainer의
  명시적 `AGENT_REWORK_RETRY` comment 이후에만 BLOCKED → READY → claim →
  agent-working으로 진입 (테스트 84/85로 고정, live 소비는 다음 사람 신호 시점).

## Rollback

- Git: 이 PR revert/`git revert` (edge + tests + docs 동시)
- Live: `automation/hermes/scripts/deploy-intake-edge.sh`가 남긴 timestamped
  backup(`.bak-kanban-github-sync-*`)을 `mv`로 복원 — cron은 건드리지 않음
