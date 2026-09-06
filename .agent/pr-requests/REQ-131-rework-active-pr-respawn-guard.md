# REQ-131: rework task active_pr respawn guard bypass & comment self-heal

- Status: Implementation
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_SYNC_REWORK`
- Integration target branch: `main`
- Required work branch: `hotfix/rework-active-pr-respawn-guard`
- Source-of-truth base: `origin/main` `bb35c3a4549b9a312717b2ba41cafc1949826c04`
- Remote delivery: Required
- Merge authority: Human/user only
- Provenance: Operator incident 2026-09-06 KST (`t_5b20836c` stranded in `ready`)
- Automation stop state: `DEPLOYED_AND_PR_OPENED`

## Objective

Main Agent가 생성한 bounded rework 개발 카드(또는 unrun 작업 명세 카드)가 `task_comments` 내 기존 PR URL로 인해 코어 디스패처의 `active_pr` respawn guard에 영구 차단(`respawn_guarded {reason: "active_pr"}`)되어 `ready`에서 적채되는 결함을 엣지 레벨에서 근본 수정하고 배포한다.

기존 초기 구현 카드의 실제 중복 PR 생성 방지 가드 및 인테이크 루트 카드의 상태 머신 동작에는 일절 영향을 주지 않는다.

## Confirmed root cause

1. **Trigger**: Main Agent가 `t_5b20836c`를 생성한 후 등록한 durable rework contract 댓글(Comment ID 330) 본문에 `Existing PR #129: https://github.com/rhgo1749/hermes-n8n-control-plane/pull/129.`이 포함됨.
2. **Core Respawn Guard**: Hermes 코어(`hermes_cli/kanban_db_dispatch.py::check_respawn_guard`)는 `task_comments`에 24시간 내 GitHub PR URL 정규식(`https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+`)이 존재하면 "이전 워커가 이미 PR을 열었음"으로 간주하고 `active_pr` 가드로 워커 스폰을 거절함.
3. **Dead Zone**: 엣지 전용 rework 디스패치 레인(`_dispatch_pending_rework`)은 오직 `governing == "github_pr_rework"`인 인테이크 루트 카드만 스폰하므로, Main이 분할 생성한 자식 개발 카드(`governing == "created"`)는 엣지 레인에서도 스킵되고 게이트웨이 디스패처에서도 `active_pr`로 차단되어 무한 적채됨.

## Scope

1. **`edge/kanban_workspace_admission.py` (Layer 1 - Self-Healing)**:
   - `repair_workspace_drift` / `preview_workspace_drift`에 `active_pr` 댓글 정규화(`_comment_active_pr_scan`) 추가.
   - 대상: 비인테이크 구현/rework 카드 또는 실행 이력 0회(`COUNT(task_runs) == 0`)인 미실행 카드.
   - `task_comments` 내 raw PR URL(`https://github.com/<owner>/<repo>/pull/<n>`)을 `<owner>/<repo>#<n>`으로 자동 치환하고 `comment_repaired` 이벤트 기록.
   - 인테이크 루트 및 실제 실행 완료 후 PR을 등록한 일반 구현 카드는 보존(기존 가드 유지).
2. **`edge/kanban_dynamic_resource.py` (Layer 2 - Dispatcher Overlay)**:
   - `install_core_claim_admission` 및 `install_everywhere`에서 `_install_respawn_guard_overlay()` 등록.
   - `kanban_db_dispatch.check_respawn_guard`를 래핑하여 rework 마커 또는 0-run 카드에 대한 `active_pr` 가드를 면제(`return None`).
   - 인테이크 루트 및 기존 실행 이력이 있는 일반 구현 카드는 `active_pr` 반환 유지.
3. **`automation/hermes/scripts/deploy-intake-edge.sh` (Layer 3 - Deployment Sync)**:
   - 배포 대상 스크립트에 `kanban_dynamic_resource.py`를 포함하여 원자적 교체 및 해시 검증 보장.

## Non-goals

- Hermes 코어(`/ws/hermes-agent`) 직접 수정 금지.
- 실제 워커가 PR을 생성한 후 `ready`로 돌아간 일반 카드의 중복 방지 가드 해제 금지.
- 인테이크 루트 카드의 코어 디스패치 허용 금지(인테이크 루트는 엣지 전용).
- 머지/자동 머지 금지(인간 머지 권한 유지).

## Validation contract

- `edge/test-kanban-workspace-selfheal.py`: 38 passed, 0 failed.
- `edge/test-kanban-dynamic-resource.py`: 18 passed, 0 failed.
- `edge/test-kanban-workspace-admission.py`: 45 passed, 0 failed.
- `edge/test-kanban-github-sync-rework.py`: 916 passed, 0 failed.
- `py_compile` (전체 변경 파일): PASS.
- `git diff --check`: PASS.
- 라이브 배포 및 카나리 검증:
  - `deploy-intake-edge.sh` 실배포 완료.
  - `t_5b20836c` 댓글 정규화 및 `check_respawn_guard` `None` 확인.
  - 디스패처 틱에서 `kanban-developer` 워커 스폰 확인.
