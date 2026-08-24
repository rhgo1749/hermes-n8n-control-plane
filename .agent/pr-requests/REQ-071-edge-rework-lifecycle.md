# REQ-071: OPEN PR 리워크 라우팅 및 종단 불변식 복구

- Status: Implementation
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `EDGE_REWORK`, `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `fix/edge-rework-lifecycle-invariants`
- Source-of-truth base: latest `origin/main`
- Remote delivery: Required
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-071-edge-rework-lifecycle.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/ctrl-hangul#74` (REQ-071)
- Kanban task ID: `t_aff9017c`
- Planning/lead owner: Hermes Kanban Main Agent
- Implementation owner: Edge reconciliation specialist
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

GitHub PR이 아직 OPEN인데 Kanban 카드가 DONE으로 종단된 뒤 `agent-rework`가
새로 붙어도 요청이 고아가 되지 않도록, edge reconciliation의 lifecycle 전이를
복구한다. 기존 PR/라벨/작업 소유권을 우회하거나 새 dispatcher를 만들지 않는다.

## Confirmed defect

- worker/reviewer 완료 후 OPEN PR이 남아 있어도 카드가 provisional DONE에 머무를
  수 있었다.
- 활성 worker의 `agent-working`과 새 `agent-rework`가 동시에 존재하면 기존 코드는
  무조건 `lifecycle_label_conflict`로 fail-closed하여 새 요청을 소비하지 않았다.
- 명시적 `AGENT_REWORK_RETRY`는 REVIEW/BLOCKED 경로에만 연결되어 DONE + OPEN PR
  카드의 재시도 요청이 intake되지 않았다.
- label timestamp와 DB governing event의 기록 시점 차이로 현재 라운드 라벨을
  신규 라운드로 오인할 수 있어, 이벤트에 `label_added_at`을 보존해야 한다.

## In scope

1. `DONE + OPEN PR` 불변식 sweep: 카드가 REVIEW로 복귀하고 claim/완료 필드를
   정리한다.
2. 활성 worker 중에는 두 라벨을 보존하고 defer하며, run 종료 후 stale
   `agent-working`만 제거하고 새 `agent-rework`를 정상 intake한다.
3. DONE + OPEN PR에서 신뢰된 exact `AGENT_REWORK_RETRY`를 bounded new-round
   ingress로 허용한다.
4. 현재 라운드의 label timestamp를 governing event에 저장해 stale/new 판정을
   정확히 한다.
5. edge lifecycle 문서와 회귀 테스트를 갱신한다.

## Explicit non-goals

- Hermes core, n8n workflow, Kanban DB schema, second dispatcher 수정
- GitHub Actions 활성화/추가
- PR #74의 merge를 자동화된 edge 로직의 완료 증거로 사용
- 기존 PR을 닫거나 새 PR로 대체
- unrelated cleanup/refactor

## Validation contract

- `python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py`
- `python3 edge/test-kanban-github-sync-rework.py` (`738 passed, 0 failed`)
- `git diff --check`
- Pyright: changed-file logic errors resolved; remaining two diagnostics are
  environment-only missing imports for machine-local `hermes_cli` modules.
- GitHub-hosted Actions are disabled by repository policy; local validation is
  authoritative for this change.

## Deployment / stop state

Repository change is delivered through the existing
`automation/hermes/scripts/deploy-intake-edge.sh` path after PR review. Runtime
cutover and PR #74 recovery require fresh operator validation; they are not
claimed by this request as merge evidence.
