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

기존 registry는 live board의 `tasks.idempotency_key`만 repo→board authority로 사용했다. 최초 Issue intake 전에는 provenance가 존재할 수 없으므로 새 저장소는 board association을 얻지 못하고 intake에 진입할 수 없었다.

실제 관찰 대상 `rhgo1749/H4V3-Meowcore`에서 canonical empty-board bootstrap을 적용한 뒤 host intake가 성공했고, task `t_4093eec7`이 실제로 claim/spawn되어 worker PID까지 기록됐다. 이후 worker가 디자인/기술 review gate를 부모 task로 연결하고 `dependency_wait`로 root를 TODO에 둔 것은 정상 dependency gating이었다.

Host acceptance 중 추가로 확인된 registry 결함은 checkout 경로였다. `h4v3-meowcore/board.json`은 정식 `default_workdir=/ws/projects/H4V3-Meowcore`를 선언하고 그 경로가 정확한 GitHub remote를 가리키는데, registry가 canonical slug만으로 `/ws/projects/h4v3-meowcore`를 강제하여 불필요한 중복 checkout이 필요했다.

## In scope

1. durable task provenance를 기존 최우선 repo→board authority로 유지한다.
2. provenance가 없는 최초 상태에서만 canonical slug와 정확히 일치하는 live board가 있고 그 board의 GitHub repo provenance가 비어 있을 때 bootstrap한다.
3. 다른 repo provenance가 있는 canonical board는 fail-closed한다.
4. resolved board가 `board.json.default_workdir`를 선언하면 checkout LOCATION으로 사용한다.
5. board metadata가 없는 legacy board만 기존 `/ws/projects/<canonical_slug>` 경로로 fallback한다.
6. 어떤 checkout 경로를 선택하든 `origin`이 대상 GitHub repository와 일치해야 `ready=true`가 된다.
7. resolver/checkout 회귀 테스트와 canonical registry 문서를 갱신한다.

## Explicit non-goals

- registry에서 board 생성/삭제
- registry에서 checkout clone/provision
- Hermes core 수정
- n8n/edge dispatcher 재설계
- fresh READY + `agent-rework` 전용 dispatch 우회 추가
- 저장소명 override table 추가
- legacy 비정규 board 이름을 canonical slug로 강제 migration
- GitHub Actions 활성화
- merge/auto-merge

## Ownership / safety

- GitHub: opt-in topic / Issue durable evidence
- Hermes Kanban: board/task lifecycle 및 board `default_workdir` authority
- registry: read-only discovery/routing only
- edge/intake: 기존 dispatcher/intake ownership 유지
- board metadata는 checkout 위치만 선택하며 repo→board association을 만들지 않음
- checkout repository identity는 독립적인 `git origin` 검증으로 fail-closed
- 새 DB/state/store 없음
- host/network/auth/secret boundary 변경 없음

## Validation contract

실행 대상:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_repository_registry.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_repository_registry_bootstrap.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_repository_registry_board_workdir.py
python3 -m py_compile \
  automation/n8n/scripts/repository_registry.py \
  tests/test_repository_registry_bootstrap.py \
  tests/test_repository_registry_board_workdir.py
git diff --check origin/main...HEAD
```

이 GitHub-direct 세션에서 실제 실행 가능한 검증만 PASS로 기록한다. 전체 repository checkout/runtime이 필요한 gate는 NOT RUN으로 남기고 operator acceptance에 넘긴다.

## Operator acceptance

PR candidate를 host checkout에 fetch한 뒤 registry가 `H4V3-Meowcore`를 다음처럼 반환하는지 확인한다.

최초 intake 전:

```text
board=h4v3-meowcore
board_status=resolved_empty_canonical_board
checkout=/ws/projects/H4V3-Meowcore
checkout_status=verified
ready=true
```

첫 Issue intake 후 다음 snapshot에서는:

```text
board_status=resolved_task_provenance
checkout=/ws/projects/H4V3-Meowcore
checkout_status=verified
ready=true
```

로 수렴해야 한다.

Host evidence already established:

- canonical board exists
- canonical board `default_workdir=/ws/projects/H4V3-Meowcore`
- that workdir is a valid Git checkout of `rhgo1749/H4V3-Meowcore`
- first intake created `t_4093eec7`
- core dispatcher claimed/spawned the root task (`pid=162798`)
- root later moved to TODO only after explicit design/technical dependency links and `dependency_wait`

따라서 fresh READY `agent-rework` normalization은 요구사항이 아니며 추가하지 않는다.

canonical live board 자체가 없다면 이 REQ는 board를 생성하지 않으므로 기존 Hermes board provisioning 절차가 별도로 필요하다.

## Completion criteria

- [x] empty canonical live board bootstrap
- [x] non-canonical board 추측 금지
- [x] conflicting canonical board fail-closed
- [x] durable provenance 우선
- [x] legacy noncanonical board provenance 회귀 보호
- [x] resolved board `default_workdir` checkout 지원
- [x] checkout origin mismatch fail-closed
- [x] canonical docs 갱신
- [x] remote Draft PR
- [ ] final host candidate validation on latest head
- [x] merge performed: NO
