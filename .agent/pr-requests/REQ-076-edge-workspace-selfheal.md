# REQ-076: 엣지 workspace 격리 드리프트 셀프힐링

## Source

- Source Issue: rhgo1749/hermes-n8n-control-plane#76
- Kanban provenance: Main Agent 직접 구현 (사용자 지시 "크론 없이 issue driven req로 셀프힐링")
- Base: origin/main @ `a9aadafa48617ffa33df1f3702d7a2d4ac4a7d36`
- Branch: `fix/issue-76-workspace-selfheal` (격리 worktree)

## 배경

2026-08-26 다중 보드 복구(t_b2d1acea capability block, t_acd36c65/t_146e3645 공유 checkout 카드)에서
확인된 결함군의 재발방지. 생성 시점(pre_tool_call 훅)과 스폰 시점(spawn admission 오버레이) 강제는
이미 존재하지만, **이미 존재하는 비종단 카드**의 바인딩 드리프트는 수동 개입이 필요했다.

## Scope

1. `edge/kanban_workspace_admission.py` — 신규 오버레이:
   - 스폰 시점 하드게이트(`_dispatch_pending_rework` 래핑): 격리 위반 구현카드 스폰을
     SIGTERM + reclaim + durable `spawn_blocked` 이벤트로 차단
   - 셀프힐링 패스(`repair_workspace_drift`): todo/ready/blocked 비종단 구현카드의
     격리 위반 바인딩을 `<repo-anchor>/.worktrees/<task-id>` + `wt/<task-id>`로 결정론 재바인딩,
     `workspace_repaired` 감사 이벤트(before/after) 기록
   - 앵커 산정 순위: idempotency_key GitHub provenance → 기존 repo-root 형태 경로 → 보드 default_workdir.
     증명 불가 시 fail-closed(리포트만, 손대지 않음)
2. `edge/kanban-github-sync.py` — `sync_board()` 진입부에 셀프힐 패스 탑재(웹훅/온디맨드 웨이크마다 실행).
   dry-run은 관측 전용. 결과 JSON 선두에 selfheal 엔트리 노출.
3. `edge/kanban-github-sync-entrypoint.py`, `automation/hermes/scripts/deploy-intake-edge.sh` — 배포 대상 추가

## Non-goals

- Hermes 코어 수정 없음
- 주기 폴링 크런 추가 없음 (엣지 웨이크에만 탑재 — 이벤트 기반)
- running/review/done/archived 카드 및 활성 claim 불간섭
- 머지/배포 자동화 금지 (인간 권한)

## 검증 프로파일 (실측)

| 게이트 | 결과 |
|---|---|
| test-kanban-workspace-selfheal.py (신규) | 11 passed, 0 failed |
| test-kanban-workspace-admission.py | 12 passed, 0 failed |
| test-kanban-github-sync-rework.py | 738 passed |
| test-kanban-resource-admission.py | 18 passed |
| completion / dependency-gate / parking / race-gate / head-binding / retry-guard / dynamic-resource | ALL PASS (738/20/10 등) |
| py_compile (전체 변경 파일) | PASS |
| git diff --check | PASS |
| **라이브 카나리아** | 시드 위반 카드 → 실제 sync_board 1회 웨이크 → 재바인딩+감사 이벤트 확인, 2차 웨이크 멱등(빈 결과) |

## 최종 자동화 정지 상태

- 로컬 검증: PASS (상표 참조)
- GitHub Actions: 저장소 정책상 NOT RUN (비용 미운영)
- PR 생성/머지: 인간 권한 — 본 REQ는 PR 생성 후 정지
