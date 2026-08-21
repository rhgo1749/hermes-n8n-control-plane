# REQ-KANBAN-REWORK-GRAPH-INVARIANTS: Kanban Rework Graph Invariant 및 GitHub-backed 완료 판정 계약 강화

- Status: In Progress
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `DOCS_ONLY`
- Validation profiles: `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `feat/kanban-rework-graph-invariants`
- Source-of-truth base: latest fetched `origin/main`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-KANBAN-REWORK-GRAPH-INVARIANTS.md`
- Merge authority: Human/user only
- Source issue: none (Direct control-plane maintenance request from Issue #71 / PR #8 orchestration review)
- Automation stop state: `NONE`

## 0. Mandatory repository route

1. `AGENTS.md`
2. `README.md`
3. `docs/KANBAN_ROLE_CONTRACTS.md`
4. `automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py`
5. `tests/test_intake_completion_contract_entrypoint.py`

## 1. Objective

Downstream Reviewer 카드가 REWORK 판정 시 직접 자식 태스크를 생성하거나 의존성 교착(`parents_not_done`)을 유발하지 않도록 Rework 그래프 불변식(Rework Graph Invariant)을 명문화하고, GitHub-backed Root 카드의 provisional completion과 authoritative done 경계를 보강한다.

## 2. Confirmed background

- Current behavior:
  - Downstream Reviewer 카드가 REWORK 발견 시 review-lane 전용 `kanban_request_changes` 실패 후 스스로 blocked가 된 상태에서 자식 개발 태스크를 생성하여 `parents_not_done` 의존성 교착(Deadlock)이 발생함.
  - `promote --force`로 일시 승격해도 Dispatcher의 claim 시점에 부모 미완료(`parents_not_done`)로 즉시 `todo`로 되돌아감.
  - Main Agent가 GitHub-backed Root 카드를 내부 하위 그래프 완료만으로 직접 done 처리 시, Edge Sync의 unmerged review 투영 전까지 일시적으로 Done으로 잘못 인식되는 혼선이 발생함.
- Target behavior:
  - Reviewer는 판정(PASS/REWORK)과 구조화된 handoff만 반환하고, 후속 태스크 생성이나 의존성 조작을 금지.
  - Main Agent가 Rework 그래프 생성을 전담하며, non-terminal/blocked Reviewer 카드를 개발 Rework 카드의 gating parent로 연결하지 않음.
  - `promote --force` 대신 정규 Kanban/control-plane 작업(unlink/link/reassign)으로 토폴로지를 복구하고, 직접 DB 수정은 수동 operator recovery로 제한.
  - GitHub-backed Root 카드의 `kanban_complete`는 provisional handoff이며, 최종 `done`은 GitHub API의 `merged_at != null` 확인 후 Edge Reconciler가 소유함을 명확히 함.

## 3. Scope / Non-goals

- In scope:
  - `docs/KANBAN_ROLE_CONTRACTS.md`: Rework graph invariant 및 canonical dependency recovery, GitHub-backed completion boundary 상세 명문화.
  - `automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py`: `_NEW_LEAD_CONTRACT` 템플릿에 Rework graph invariant 및 provisional done 계약 반영.
  - `tests/test_intake_completion_contract_entrypoint.py`: 갱신된 계약 텍스트에 대한 회귀 테스트 보강.
- Non-goals:
  - Hermes 코어 수정.
  - GitHub Actions 워크플로 활성화.
  - 임의의 자동 머지.

## 4. Acceptance criteria

- [x] `docs/KANBAN_ROLE_CONTRACTS.md`에 Rework graph invariant 및 canonical recovery 규칙이 명시됨.
- [x] `_NEW_LEAD_CONTRACT` 템플릿에 Reviewer의 자식 생성 금지 및 Main Agent의 올바른 Rework 그래프 구성 규칙이 반영됨.
- [x] `tests/test_intake_completion_contract_entrypoint.py` 및 전체 pytest 스위트가 통과함.
- [x] 배포 스크립트(`deploy-intake-edge.sh`)를 통한 정상 배포 및 py_compile 검증 완료.
