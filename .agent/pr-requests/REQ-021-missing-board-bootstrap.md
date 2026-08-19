# REQ-021: 신규 hermes-agent 저장소의 첫 Kanban board bootstrap 교차 해소

- Status: Draft
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `fix/registry-first-intake-board-bootstrap`
- Source-of-truth base: `origin/main` @ `9d414bab2c04ea027cbee2ebea1670ec0cd2a9ee`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-021-missing-board-bootstrap.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#21`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/21
- Kanban task ID: `t_a240fad9`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:21`
- Planning/lead owner: kanban-main (Hermes)
- Implementation owner: kanban-main (Hermes)
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## 1. Objective

`hermes-agent` topic opt-in + verified checkout가 통과한 신규 저장소가 기존 Kanban
task provenance가 없단 이유만으로 `board_not_found_task_provenance`로 `ready=false`
고정되어 첫 `agent-ready` Issue intake가 영구적으로 불가능해지는 bootstrap 교차를
해소한다.

기존 board provenance가 있으면 현행 동작을 그대로 유지하고, provenance가 전혀
없는 신규 opted-in repository만 canonical repository slug를 **최초 board
bootstrap identity**로 사용한다.

## 2. Confirmed background

- Current behavior: `_resolve_board`는 durable `tasks.idempotency_key`
  provenance가 없고 canonical board도 없으면 `not_found_task_provenance`를
  반환하며, `build_entry`는 `ready=false` + `board_not_found_task_provenance`를
  기록한다. registry는 read-only라서 board를 만들지 않으므로, 첫 task가
  만들어지기 전까지 이 상태가 영구화된다.
- Defect/limitation: 첫 `agent-ready` Issue를 Kanban에 넣으려면 기존 task
  provenance가 필요하고, 그 provenance를 만들려면 첫 Issue가 먼저 Kanban에
  들어가야 하는 bootstrap 교차. Issue #21 관찰 상태:
  `board: null / board_status: not_found_task_provenance /
  checkout_status: verified / ready: false`.
- Repository evidence:
  - PR #20(merged)이 1단계(provenance-first resolver + empty canonical board
    admission)를 구현·host-검증했다. 이 PR은 2단계다.
  - 유지보수자 Issue #21 코멘트(2026-08-14): "#21 구현 시 registry는
    missing-board bootstrap intent를 명시적으로 반환하고, intake만 mutation
    owner가 되도록 한다. ambiguous/mixed/canonical conflict는 계속
    fail-closed한다."
- Assumptions: board provisioning은 Hermes의 기존 board creation surface
  (`hermes kanban boards create <slug>`)만 사용한다.

## 3. In scope

1. registry는 board를 생성하지 않는다(read-only 유지). 다만
   `checkout_status=verified`이고 provenance·canonical board가 없는
   `not_found_task_provenance` 상태의 entry에만 명시적 read-only bootstrap
   intent(`board` = canonical slug, `checkout` = 검증된 checkout 경로)를
   반환한다.
2. canonical board conflict / ambiguous provenance / unverified checkout
   (missing·not_directory·origin_unavailable·remote_mismatch)는 bootstrap
   intent를 갖지 않고 계속 fail-closed 한다.
3. 기존 legacy board association(`ctrl-hangul -> ctrlhangul` 등)과
   `resolved_task_provenance` / `resolved_empty_canonical_board` 동작은
   변경하지 않는다.
4. intake가 registry bootstrap intent를 소비해 canonical board를
   `hermes kanban boards create <canonical-slug> --default-workdir
   <verified-checkout>`으로 **idempotent**하게 1회 provision한다(기존 board
   존재 시 아무것도 하지 않음). 생성 후 board가 실제로 landed했는지 검증하고
   실패 시 fail-closed.
5. provisioning 후 같은 tick 내 registry snapshot을 다시 로드해 생성된 empty
   canonical board가 empty-board rule로 resolved되고 첫 task가 같은 tick에
   생성되도록 한다.
6. 첫 task 생성 후 `tasks.idempotency_key` provenance가 장기 association
   authority로 다시 수렴하도록 한다(bootstrap intent 소멸).
7. `tests/test_repository_registry.py` 및 intake regression 추가/통과.
8. `docs/REPOSITORY_REGISTRY.md`가 실제 bootstrap 계약과 일치하도록 갱신.

## Explicit non-goals

- 기존 legacy board association 변경
- ambiguous/mixed provenance의 fail-closed 완화
- canonical slug board가 다른 repository provenance를 소유하는 허용
- 새 task DB / registry DB / manual allowlist / override table 추가
- Hermes core 수정 (기존 `hermes kanban boards create` surface만 사용)
- task lifecycle authority 복제 (intake는 기존 `hermes kanban create` surface로
  task 생성만 담당)
- GitHub Actions 활성화
- merge/auto-merge (human/user only)

## 4. Validation

| Test | Result | Notes |
|---|---|---|
| `python3 tests/test_repository_registry.py` | PASS | bootstrap intent 필드 + fail-closed 상태 회귀 포함 |
| `python3 tests/test_repository_registry_bootstrap.py` | PASS | PR #20 기존 bootstrap 회귀 무변경 |
| `python3 tests/test_repository_registry_board_workdir.py` | PASS | workdir 회귀 무변경 |
| `python3 tests/test_repo_scoped_intake.py` | PASS | provisioning idempotency·scope·fail-closed·same-tick first-task 회귀 포함 |
| `python3 automation/n8n/scripts/validate.py` | PASS | n8n workflow 정적 검증 |

## 5. Deploy / rollback

- Deploy: `automation/hermes/scripts/deploy-intake-edge.sh`로 intake + registry
  복사본을 `$HERMES_HOME/scripts`에 배포(기존 candidate copy → validate →
  atomic replace 절차). cron job `default:bf431b2a6ba6`의 id/schedule/enabled는
  변경하지 않는다.
- Rollback: deploy 시 생성된 `.bak-<name>-<ts>` 백업으로 원복(deploy 출력을
  참조).
- Production acceptance(HOST_VALIDATION_REQUIRED): 신규 opted-in repository에
  대해 첫 intake tick이 canonical board를 정확히 1회 생성하고, 재실행 tick에서
  idempotent하게 아무 board도 생성하지 않으며, 첫 task 생성 후 registry가
  `resolved_task_provenance`로 수렴하는 것을 host에서 확인해야 한다. 이
  검증은 human/user가 실제 신규 저장소 대상 host에서 수행한다.

## 6. Ownership / safety

- GitHub: opt-in topic / Issue durable evidence (read-only)
- Hermes Kanban: board/task lifecycle 및 board `default_workdir` authority —
  provisioning은 기존 `hermes kanban boards create` surface만 사용
- registry: read-only 유지, bootstrap intent만 반환
- intake: board provisioning + task 생성의 유일한 mutation owner
