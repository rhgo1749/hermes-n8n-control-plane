# REQ-019: 신규 repository registry bootstrap deadlock 해소

- Status: Draft
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `fix/repository-registry-canonical-bootstrap`
- Source-of-truth base: `origin/main` @ `7dc746fd2918e955b7028c4f00f6bb728f69940d`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-019-repository-registry-bootstrap.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#19`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/19`
- Kanban task ID: unavailable at request creation
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:19`
- Planning/implementation owner: ChatGPT GitHub direct session
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## Objective

`hermes-agent` topic으로 opt-in한 신규 저장소가 기존 GitHub-backed Kanban task가 없다는 이유만으로 registry `ready=false`에 영구 고정되는 bootstrap deadlock을 해소한다.

## Confirmed cause

현재 registry는 live board의 `tasks.idempotency_key`만 repo→board authority로 사용한다. 최초 Issue intake 전에는 provenance가 존재할 수 없으므로 새 저장소는 board association을 얻지 못하고 intake에 진입할 수 없다.

실제 관찰 대상 `rhgo1749/H4V3-Meowcore`는 topic `hermes-agent`, Issue #2 `agent-ready`, PR #3 `agent-rework` 조건을 충족하지만 이 bootstrap 경계에서 막힌다.

## In scope

1. durable task provenance를 기존 최우선 authority로 유지한다.
2. provenance가 없는 최초 상태에서만 canonical slug와 정확히 일치하는 live board가 있고 그 board의 GitHub repo provenance가 비어 있을 때 bootstrap한다.
3. 다른 repo provenance가 있는 canonical board는 fail-closed한다.
4. resolver 회귀 테스트와 canonical registry 문서를 갱신한다.

## Explicit non-goals

- registry에서 board 생성/삭제
- Hermes core 수정
- n8n/edge dispatcher 재설계
- 저장소명 override table 추가
- legacy 비정규 board 이름을 canonical slug로 강제 migration
- GitHub Actions 활성화
- merge/auto-merge

## Ownership / safety

- GitHub: opt-in topic / Issue durable evidence
- Hermes Kanban: board/task lifecycle authority
- registry: read-only discovery/routing only
- edge/intake: 기존 dispatcher/intake ownership 유지
- 새 DB/state/store 없음
- host/network/auth/secret boundary 변경 없음

## Validation contract

실행 대상:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_repository_registry_bootstrap.py
python3 -m py_compile automation/n8n/scripts/repository_registry.py tests/test_repository_registry_bootstrap.py
python3 tests/test_repository_registry.py
git diff --check
```

이 GitHub-direct 세션에서 실제 실행 가능한 검증만 PASS로 기록한다. 전체 repository checkout이 필요한 gate는 NOT RUN으로 남기고 operator acceptance에 넘긴다.

## Operator acceptance

PR candidate를 host checkout에 fetch한 뒤 registry가 `H4V3-Meowcore`를 다음 중 하나로 반환하는지 확인한다.

```text
board=h4v3-meowcore
board_status=resolved_empty_canonical_board   # 최초 intake 전
ready=true
```

첫 Issue intake 후 다음 snapshot에서는:

```text
board_status=resolved_task_provenance
```

로 승격되어야 한다.

canonical live board 자체가 없다면 이 REQ는 board를 생성하지 않으므로 기존 Hermes board provisioning 절차가 별도로 필요하다.

## Completion criteria

- [ ] empty canonical live board bootstrap
- [ ] non-canonical board 추측 금지
- [ ] conflicting canonical board fail-closed
- [ ] durable provenance 우선
- [ ] legacy noncanonical board provenance 회귀 없음
- [ ] canonical docs 갱신
- [ ] remote Draft PR
- [ ] merge performed: NO
