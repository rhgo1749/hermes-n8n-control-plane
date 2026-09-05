# REQ-095: rework dispatcher runtime API 호환성 복구

- Status: 로컬 검증 완료 / 별도 hotfix 리뷰 대상
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`
- Source Issue: [Issue #95](https://github.com/rhgo1749/hermes-n8n-control-plane/issues/95)의 runtime 선행 장애 복구. Issue 전체 구현/종료를 의미하지 않는다.
- Existing implementation PR: [PR #115](https://github.com/rhgo1749/hermes-n8n-control-plane/pull/115), 수정/머지하지 않음.
- Kanban provenance: 기존 개발 카드 `t_f05626f0`; 이 hotfix는 사용자 직접 요청으로 수행하며 해당 카드의 worker run/delivery를 사칭하지 않는다.
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:95`
- Source-of-truth base: fetched `origin/main` = `93d6daa4e99b61c3d4fe7f8b825d19ae0995c5e1`
- Integration target: `main`
- Work branch: `hotfix/edge-dispatch-api-compat`
- Remote delivery: Required, 별도 PR / merge·auto-merge는 사용자 권한
- Automation stop state: `HUMAN_VALIDATION_REQUIRED` (PR review/merge)

## 근거 및 경계

Route: `AGENTS.md` → `README.md` → `docs/README.md` →
`docs/EDGE_REWORK_LIFECYCLE.md`; 배포는 `docs/OPERATIONS.md` 및
`automation/hermes/scripts/deploy-intake-edge.sh` 계약을 따른다.

현재 Hermes revision `f159e581c7afd22a5c94652c569e3859f1b994d2`에서
rework/head-binding 회귀 검사가 `kanban_db._default_spawn` /
`kanban_db._record_task_failure` AttributeError로 실패했다. 추가 경로 조사에서
lock, workspace, PID 기록, spawn 실패 처리도 같은 구현 분리의 영향을 확인했다.

- dispatch/failure/PID owner: `hermes_cli.kanban_db_dispatch`
- worktree resolver owner: `hermes_cli.kanban_db_workspace`
- dispatch lock owner: `hermes_cli.kanban_db_connect`
- 삭제된 `_record_spawn_failure` 동작은 현재 통합 `_record_task_failure`의
  `outcome="spawn_failed", release_claim=True, end_run=True`로 보존한다.
- 미설정 failure limit은 runtime `DEFAULT_FAILURE_LIMIT`을 사용하며,
  per-task `max_retries` 우선순위와 회로 차단을 유지한다.
- workspace overlay는 gate 통과 후에만 현재 default spawn을 해석한다.
  주입 callback과 legacy callback 우선순위 및 fail-closed sentinel은 유지한다.

Ownership/security/auth/network 영향 없음. 새 DB/dispatcher 없음.
Hermes core, n8n workflow/Compose/actuator, credential, cron schedule,
기존 #95 worktree와 PR #115, #117/#118은 변경하지 않는다.
수동 unblock, completion marker 게시, 실제 검증 worker spawn은 하지 않는다.

## 기존 테스트 변경 근거

행동 계약과 assertion은 완화하지 않는다.

1. rework/actual-core 테스트 spy를 삭제된 facade가 아닌 현재 구현 owner에 설치한다.
   없는 private API를 fixture가 다시 만드는 방식으로 호환성 오류를 숨기지 않는다.
2. actual-core workspace fixture에는 현재 `_validate_rework_event`가 요구하는
   positive `rework_round`와 full 40-character `head_sha`를 제공한다.
   기존 불완전 fixture는 workspace gate보다 앞에서 `rework_event_round_invalid`로
   종료되어 의도한 계약을 검증하지 못했다. 이는 production gate를 완화하는 변경이
   아니라 이미 적용된 유효 event 계약에 입력을 맞추는 변경이다.
3. workspace 위반 시 ZERO spawn, ZERO failure accounting, durable event 및
   event-write-failure fail-closed assertion을 모두 보존하고 45개 assertion 통과를 확인한다.
4. 추가 `test-kanban-dispatch-runtime.py`는 실제 Hermes + 격리 board에서 current owner만
   mock한다. lock/PID/worktree/default spawn/failure cleanup/circuit breaker/runtime
   default/per-task override를 검증하며 실제 worker나 GitHub API를 호출하지 않는다.

## 실제 로컬 검증

공통 runtime: `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1`
및 설치된 Hermes Python interpreter. 초기 실패를 기록한 뒤 같은 환경에서 재검사했다.

| 검사 | 결과 |
| --- | --- |
| `edge/test-kanban-github-sync-rework.py` | 881 passed, 0 failed |
| `edge/test-kanban-head-binding-feedback.py` | 881 passed, 0 failed (동일 rework harness 실행; 별개 독립 881개로 합산하지 않음) |
| `edge/test-kanban-github-sync-completion.py` | ALL COMPLETION REGRESSIONS PASS |
| `edge/test-kanban-dispatch-runtime.py` | 8 tests OK |
| `edge/test-kanban-workspace-admission.py` | 45 passed, 0 failed; actual-core phase 실행 |
| attention-delivery-recovery / delivery-provenance-guard / edge-projection-label-history | 각각 5 / 5 / 4 tests PASS |
| Ruff `F821,F822,F823`, `git diff --check` | PASS |
| Pyright (실제 Hermes import 경로 사용) | production 변경 2파일 + 새 테스트 오류 0; 기존 harness 포함 32 → 18 diagnostics, 신규 diagnostic 및 missing import 0 |
| deploy candidate `--dry-run` | PASS; live hook config와 candidate의 의미적 내용 동일 |

추가 검사 `edge/test-kanban-rework-attention-selfheal-label.py`는 첫 assertion에서
실패한다. **변경 전 clean main과 동일하게 재현**되며 해당 테스트/entrypoint는 이
hotfix에서 변경하지 않았다. 전체 suite PASS라고 주장하지 않는다.
기존 harness의 나머지 타입 진단 및 public compatibility shim 경고는 별도 후속 범위다.
GitHub-hosted Actions는 저장소 정책상 비활성화이며 PASS로 표기하지 않는다.

## 배포/인수 계약

검증한 별도 hotfix commit을 원격 PR에 보존한 후 배포한다. main에 머지되었다고
주장하지 않는다. `deploy-intake-edge.sh --hermes-home <active-hermes-home>`으로
candidate compile/help, dependency-first atomic replace, timestamped backup,
설치 byte hash 검증을 수행한다. 대상 config는 기존 hook 의미를 바꾸지 않는다.
배포 후 source/live SHA-256 및 wrapper가 로드하는 core 경로를 재조회하고,
실제 설치 wrapper를 격리 board/mock worker로 검증한다. 실제 운영 dry-run 결과와
host health는 이 unit 검증과 구분해 PR body/final report에 기록한다.

이 수정은 edge 파일 변경만 필요하다. 호스트 Docker/systemd 재시작 또는 workflow
재import가 필요한지는 별도로 live/source 차이와 health로 판정하며, 컨테이너에서
불가능한 호스트 작업을 완료로 보고하지 않는다. rollback은 배포 script가 출력한
정확한 backup 복원 명령을 사용한다. #95 unblock/completion 및 fleet intake 정상화는
이 hotfix의 파일 배포 성공만으로 달성되었다고 주장하지 않는다.
