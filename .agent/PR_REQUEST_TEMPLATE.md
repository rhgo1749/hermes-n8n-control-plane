# H4V3 Control-Plane Agentic PR Request Template

> Lineage: H4V3 Agentic PR Request pattern
> Repository specialization: `rhgo1749/hermes-n8n-control-plane`
> Purpose: GitHub Issue → Hermes Kanban → worker/lead execution → human review

이 파일은 `hermes-n8n-control-plane` 전용 **PR 요청서 생성 템플릿**이다. 실제 작업에서는 현재 Issue와 최신 저장소 상태를 조사한 뒤 필요한 항목만 남겨 `.agent/pr-requests/PR-NNN-<slug>.md`로 축약해 사용한다.

새 worker/lead는 이전 ChatGPT/Hermes/사람 대화 맥락을 공유하지 않는다고 가정한다. 구현 판단에 필요한 내용은 GitHub Issue, 이 저장소의 canonical 문서, 실제 PR 요청서, Git history/PR, 실제 runtime evidence 중 하나에서 복구 가능해야 한다.

---

# PR-NNN: 한글 제목

- Status: Draft
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `EDGE_RECONCILIATION` / `HERMES_PLUGIN` / `N8N_WORKFLOW` / `DOCS_ONLY`
- Validation profiles: `STATIC_UNIT` / `N8N_VALIDATE` / `EDGE_REWORK` / `HERMES_PLUGIN` / `HOST_DASHBOARD` / `HOST_NETWORKING` / `TELEGRAM_E2E` 중 필요한 최소 집합
- Integration target branch: `main`
- Required work branch: `type/short-description`
- Source-of-truth base: latest fetched `origin/main`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/PR-NNN-<slug>.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#NNN`
- Source issue URL:
- Kanban task ID:
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:NNN`
- Planning/lead owner:
- Implementation owner:
- Automation stop state: `NONE` / `HUMAN_VALIDATION_REQUIRED` / `HOST_VALIDATION_REQUIRED` / `BLOCKED`

## 0. Mandatory repository route

작업 시작 전에 다음 순서로 확인한다.

1. `AGENTS.md`
2. `README.md`
3. 현재 변경 표면의 canonical 문서
4. 대상 source/tests
5. live runtime/deployment evidence가 필요한 경우 현재 운영 상태를 읽기 전용으로 확인

대표 route:

- Overview / Need You / Telegram: `docs/H4V3_OVERVIEW.md`
- GitHub ↔ Kanban lifecycle / rework: `docs/EDGE_REWORK_LIFECYCLE.md`
- n8n rollout / cutover / rollback / host operations: `docs/OPERATIONS.md`
- GitHub event serialization: `docs/GITHUB_EVENT_CONCURRENCY.md`
- repository registry / routing: `docs/REPOSITORY_REGISTRY.md`
- deferred ideas: `docs/FUTURE_IMPROVEMENTS.md`

요청서에 적힌 route는 후보일 뿐이다. 현재 repository evidence와 충돌하면 임의로 합성하지 말고 실제 authority를 기록한다.

## 1. Objective

한 PR에서 한 개의 구체적이고 검증 가능한 결과만 정의한다.

## 2. Confirmed background

- Current behavior:
- Defect/limitation:
- Affected operators/systems:
- Repository evidence:
- Source Issue-only product intent:
- Runtime/host evidence:
- Assumptions or unresolved facts:

대화에서만 알고 있는 사실을 구현 전제로 사용하지 않는다. scope, authority, deployment path, secret/auth, runtime ownership, validation에 영향을 주는 정보가 Issue/저장소/runtime evidence에서 복구되지 않으면 `BLOCKED`로 남긴다.

### Control-plane ownership gate

다음 경계를 먼저 확인한다.

- **GitHub**: durable external work/review record — Issue/PR/code/review history
- **Hermes Kanban**: active execution/dispatch/review lifecycle authority
- **edge**: GitHub ↔ Kanban reconciliation owner
- **n8n**: scheduling/glue/triggering — second dispatcher가 아님
- **H4V3 Overview**: read-only projection — task state owner가 아님
- **Telegram**: attention/pager surface — history DB나 task store가 아님

이번 변경이 이 ownership을 바꾸는지:

- Ownership impact: NONE / AFFECTED / UNCERTAIN
- New state/store introduced: NO / YES / UNCERTAIN
- Existing authority bypassed: NO / YES / UNCERTAIN
- Required owner/document updates:

`AFFECTED`/`UNCERTAIN`이 있으면 구현 전에 현재 owner와 callers/consumers를 확인한다. 단순 편의 때문에 parallel state, second dispatcher, duplicate notification store를 만들지 않는다.

### Security / auth / secret / policy impact gate

- Security impact: NONE / AFFECTED / UNCERTAIN
- Auth/permission/secret boundary: NONE / AFFECTED / UNCERTAIN
- Host/network exposure: NONE / AFFECTED / UNCERTAIN
- External API/platform policy impact: NONE / AFFECTED / UNCERTAIN
- Required canonical docs/evidence:
- Residual risk / owner:

GitHub-hosted Actions는 저장소 정책상 의도적으로 비활성화되어 있다. 명시 요청 없이 enable/re-enable하거나 새 hosted workflow를 추가하지 않는다.

## 3. In scope

1.
2.
3.

## 4. Explicit non-goals

기본적으로 다음을 하지 않는다. Issue가 명시적으로 요구하는 경우에만 별도 검토한다.

- Hermes core 수정/fork
- 새 task DB / notification DB / parallel state store
- second dispatcher 또는 second completion owner
- n8n에서 worker/worktree/spawn/completion 재구현
- Overview에서 task mutation / worker control / Issue·PR 생성
- Telegram을 event history/database로 확장
- GitHub-hosted Actions enable/re-enable 또는 신규 hosted CI workflow 추가
- 관련 없는 cleanup/redesign/migration
- base branch 직접 commit/push
- merge/auto-merge

## 5. Implementation requirements

- 최신 `origin/main` fetch 후 authoritative base SHA를 기록한다.
- dedicated branch/worktree에서 작업한다.
- existing owner/caller/state boundary를 확인하고 가장 작은 root-cause 변경을 선호한다.
- runtime 배포 사본과 repository checkout이 분리된 경우 실제 운영 경로를 확인하고, 배포 절차/rollback을 명시한다.
- secret/token/machine-local path를 tracked source/PR/chat에 넣지 않는다.
- existing Hermes messaging path가 있는 경우 Telegram sender를 재구현하지 않는다.
- Overview는 read-only contract를 유지하며 Kanban canonical DB/schema initializer를 통해 쓰기 연결하지 않는다.
- edge lifecycle은 fresh GitHub evidence와 기존 Kanban transition contract를 우회하지 않는다.
- durable contract가 바뀔 때만 관련 canonical 문서를 같은 PR에서 갱신한다.

## 6. Validation contract

GitHub Actions가 비활성화되어 있다는 사실은 실패가 아니지만, **required local validation을 생략할 권한도 아니다.**

- `NOT RUN != PASS`
- 실제 실행하지 않은 command를 PASS로 주장하지 않는다.
- 변경 표면에 필요한 최소 validation profile만 선택한다.
- host/runtime validation과 repository/static validation을 섞지 않는다.

### `STATIC_UNIT`

대표 후보:

```bash
python3 tests/test_repo_scoped_intake.py
python3 tests/test_repository_registry.py
python3 tests/test_github_router.py
python3 tests/test_intake_lease_controller.py
python3 tests/test_cutover_snapshot_boundary.py
python3 tests/test_github_event_concurrency_contract.py
python3 tests/test_h4v3_overview.py
python3 tests/test_h4v3_notification_policy.py
python3 -m py_compile <changed-python-files>
bash -n <changed-shell-files>
git diff --check
```

실제 변경과 무관한 전체 suite를 의무적으로 돌리지 않는다. 선택한 명령과 이유를 기록한다.

### `N8N_VALIDATE`

```bash
python3 automation/n8n/scripts/validate.py
```

필요한 경우 render/import/cutover contract validation을 별도로 기록한다. 실제 host cutover를 unit/static validation로 대체하지 않는다.

### `EDGE_REWORK`

대표 regression:

```bash
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-rework.py
```

상위 환경의 `HERMES_DELEGATED_CHILD_CONTEXT=1` 때문에 mutation guard가 차단된 경우, 이를 FAIL/PASS로 오인하지 않고 clean child-marker environment에서 재실행한 사실을 보고한다.

### `HERMES_PLUGIN`

- plugin manifest/load discovery
- backend import/router registration
- existing plugin enable contract
- core tool override 금지 여부
- plugin-specific tests

### `HOST_DASHBOARD`

실제 운영 Hermes Dashboard에서만:

- exact installed candidate identity
- plugin install/backup/rollback path
- dashboard restart/reload
- authenticated browser/API path
- 실제 render/deep-link/read-only behavior

### `HOST_NETWORKING`

실제 host/container network boundary가 범위일 때만:

- bind address/port
- container ↔ host reachability
- public exposure 여부
- least-privilege/auth boundary

### `TELEGRAM_E2E`

- existing `hermes send` path 재사용
- target resolution 확인
- 실제 roundtrip은 운영 side effect임을 명시
- pure notification policy test와 transport E2E를 분리

## 7. Host/operator acceptance handoff

필수 runtime/dashboard/network/Telegram 검증이 worker authorized environment 밖에 있어 `HOST_VALIDATION_REQUIRED`로 멈출 때는 단순히 “호스트 확인 필요”라고 끝내지 않는다.

PR/final report에 반드시 남긴다.

- authoritative base SHA
- PR head SHA / exact candidate identity
- 아직 검증하지 못한 항목
- copy-paste 가능한 최소 acceptance command
- expected PASS conditions
- 실패 시 diagnostics / recovery / rollback
- PASS 후 merge-ready 여부
- 추가 human judgment 필요 여부

권한 없는 worker는 host privilege를 우회하거나 Docker socket/sudo/public exposure를 새로 요구하지 않는다.

권장 형식:

````markdown
### Operator acceptance

**Copy-paste command**

```bash
# canonical deployment/runtime path를 사용하는 최소 acceptance
```

**PASS conditions**

- [ ] exact candidate가 실제 runtime에 설치됨
- [ ] 실제 변경 경로 end-to-end 성공
- [ ] expected state/output과 일치

**If validation fails**

```bash
# 최소 diagnostics + rollback/retry
```

**After PASS**

- Merge-ready: YES / NO
- Additional human judgment required: YES / NO
````

## 8. Lead / delegation contract

Lead는:

1. Issue와 mandatory route를 먼저 읽는다.
2. current main/open/conflicting work와 source-of-truth를 조사한다.
3. 이 템플릿을 실제 요청서로 축약한다.
4. 독립적인 bounded subtask만 worker에게 delegate한다.
5. delegated output을 그대로 신뢰하지 않고 diff와 validation evidence를 다시 검토한다.
6. deterministic gate가 실패하면 수정/재위임한다.
7. 필수 host/human gate가 남으면 정확한 stop state와 operator handoff를 남긴다.
8. PR은 생성/갱신할 수 있으나 merge하지 않는다.

Delegated worker는 범위를 넓히지 않고 changed files / evidence / assumptions / failures / residual risk를 반환한다.

## 9. Understanding handoff

- Confirmed cause/need:
- Before flow:
- After flow:
- Canonical state/source owner:
- Callers/consumers:
- Ownership preserved/changed:
- Key design decision:
- Rejected alternative and reason:
- First debugging entry point:

Control-plane 변경은 가능하면 다음 형태로 표현한다.

`GitHub / n8n event → intake/edge → Hermes Kanban → worker/review → GitHub`

Operator surface 변경은 가능하면 다음 형태로 표현한다.

`Kanban/task_events → Overview projection / notification decision → dashboard or hermes send`

## 10. Completion criteria

- [ ] Source Issue / Kanban / idempotency provenance recorded
- [ ] Required context is recoverable without ephemeral chat memory
- [ ] Latest fetched `origin/main` used as base
- [ ] Dedicated branch/worktree used
- [ ] Objective completed; non-goals respected
- [ ] Correct canonical route selected
- [ ] GitHub/Kanban/edge/n8n/Overview/Telegram ownership preserved or explicitly changed
- [ ] Security/auth/secret/network/policy impact gate evaluated
- [ ] Required local validation executed truthfully; `NOT RUN != PASS`
- [ ] Required host acceptance handoff present when needed
- [ ] No unrelated/machine-local/secret/destructive changes included
- [ ] Work branch pushed and exactly one Korean PR opened/updated
- [ ] Remaining HUMAN/HOST/BLOCKED state explicit
- [ ] PR not merged without human/user authorization

## 11. Final report

### Summary
- Implemented:
- Intentionally not implemented:

### Provenance
- Source issue:
- Kanban task:
- Idempotency key:
- Request path:
- Lead/delegated workers:
- Automation stop state:

### Repository findings
- Selected route:
- Canonical owners:
- Request assumptions differing from repository/runtime evidence:

### Cross-cutting impact
- Security:
- Auth/permission/secrets:
- Host/network exposure:
- External API/platform policy:
- Follow-up / residual risk:

### Files changed
- `path`: reason

### Validation
| Validation | Result | Notes |
|---|---|---|
| Static/unit | PASS / FAIL / NOT RUN / SKIPPED | |
| n8n validation | PASS / FAIL / NOT RUN / SKIPPED | |
| Edge rework | PASS / FAIL / NOT RUN / SKIPPED | |
| Hermes plugin | PASS / FAIL / NOT RUN / SKIPPED | |
| Host dashboard/network | PASS / FAIL / NOT RUN / SKIPPED | |
| Telegram E2E | PASS / FAIL / NOT RUN / SKIPPED | |

### Operator acceptance
- Required only when automation stop state is `HOST_VALIDATION_REQUIRED`.
- Copy-paste command:
- PASS conditions:
- If validation fails / recovery:
- After PASS merge-ready:
- Additional human judgment required:

### Remaining risks / owner

### Git / PR
- Base SHA:
- Branch:
- Commits:
- PR number/title/URL:
- Working tree:
- Merge performed: NO
