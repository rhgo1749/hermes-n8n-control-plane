# REQ-073: merged Issue의 stale rework 그래프 terminal convergence

- Status: Bounded rework round 3 implemented and pushed (PR #74 open; head `8cfa929e2d7c98f0dc503d4d235d8e5481346e3e`; human review/merge pending)
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `EDGE_REWORK` (+ `STATIC_UNIT` py_compile / Pyright / diff-check)
- Integration target branch: `main`
- Required work branch: `fix/issue-73-terminal-merge-convergence`
- Source-of-truth base: latest fetched `origin/main`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-073-merged-stale-rework-terminal-convergence.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#73`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/73
- Kanban task ID: `t_2fb0b688` (bounded rework round 3; prior round `t_2b00c07a`, review `t_86d72c71`)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:73`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## 0. Mandatory repository route

1. `AGENTS.md` → 2. `README.md` → 3. `docs/README.md` →
   `docs/EDGE_REWORK_LIFECYCLE.md`(rework/terminal/review transition) →
   `docs/GITHUB_COMPLETION_LIFECYCLE.md`(DONE/REVIEW projection contract) →
   4. `edge/kanban-github-sync.py` + `edge/test-kanban-github-sync-dependency-gate.py`,
   `edge/test-kanban-github-sync-rework.py`

## 1. Objective

`edge/kanban-github-sync.py`에 **edge-only terminal convergence** pass를 추가한다:
source Issue가 closed이고 모든 required linked PR이 target branch에
merged이며 도달 가능한 dependency chain에 활성 claim/run/worker 소유가
없는 canonical GitHub Issue intake root의 stale rework 그래프를 **1개
reconciliation pass**에서 `done / archived / done`으로 수렴시킨다.
그 어떤 증거가 부족하면 그래프를 원상 보존(fail-closed)한다.

## 2. Confirmed background

- Current behavior: `sync_board()`는 GitHub 읽기 **이전**에 internal
  dependency gate를 평가한다. root의 pending child(`t_9a6395f0`) 때문에
  `internal_dependency_pending`를 반환하며 fresh merge 증거를 볼 수 없다.
  blocked developer/reviewer 카드는 canonical intake body가 아니므로
  독립 reconciling도 안 된다.
- Defect/limitation: H4V3-DJ Issue #88는 CLOSED, 유일한 closing PR #144는
  `main`에 merged(head `6669d5c8b75d758c5557b941997fd9040849d966`)지만
  그래프 `t_dba6e389(done) -> t_fbd3f0b9(blocked capability) ->
  t_9a6395f0(todo reviewer) -> t_7e4e4523(todo intake root)`가 strand된다.
- Repository evidence: live edge dry-run은 GitHub 읽기 전
  `internal_dependency_pending`를 반환. live core와
  `edge/kanban-github-sync.py`는 SHA-256
  `9224abc20470fda172b43d54afc0afd7c92b3525f705bba6e76e3d0f513b2376` 동일.
- Assumptions: 재귀(ancestor chain)는 `task_links` direct parent edge
  기준으로만 도달 가능하며, dangling edge는 lookup failure로 취급한다.

### Control-plane ownership gate

- Ownership impact: **AFFECTED**(edge reconciliation surface에만 추가
  transition. Kanban schema/core 변경 없음)
- New state/store introduced: **NO**(`task_events` durable event만 추가)
- Existing authority bypassed: **NO**(gate는 활성 작업 보호를 그대로 유지.
  본 pass는 stale shape만 수렴하며, 모든 비수렴 shape은 기존
  `internal_dependency_pending` lane으로 fallthrough)
- Required owner/document updates: `docs/EDGE_REWORK_LIFECYCLE.md`

### Security / auth / secret / policy impact gate

- Security impact: NONE / Auth: NONE / Host/network: NONE / Platform policy: NONE
- Residual risk: edge-only read+bounded write. fresh GitHub evidence가
  권위 boundary이며, local comment만으로는 completion이 생성되지 않는다.

## 3. In scope

1. `edge/kanban-github-sync.py`:
   - `_terminal_chain_ancestors`(task_links ancestor walk, dangling fail-closed)
   - `_node_has_active_ownership`(claim/run/worker)
   - `_issue_is_closed`(fresh authoritative Issue read)
   - `_terminal_convergence_evidence`(closed Issue + all linked PR merged
     into target + fresh reads)
   - `_attempt_terminal_merge_convergence`(single-pass, single-transaction
     convergence. `github_pr_sync` durable event)
   - `sync_board()` gate-pending lane에 전제(ambiguity/active-ownership/
     non-authoritative read는 classic lane으로 fail-closed fallthrough)
2. `edge/test-kanban-github-sync-terminal-convergence.py`(isolated temp
   `HERMES_HOME` + real Kanban DB layer + fake GitHub client):
   1. #88-shape graph(`done -> blocked -> todo -> todo root`)가 closed
      Issue + merged PR 증거 이후 `done/archived/done` 수렴 + durable
      event reason 검증
   2. 2차 pass idempotence + claim/spawn 없음
   3. Issue-open / open PR / closed-unmerged PR / GitHub lookup error
      보존
   4. active claim/run/worker ownership 거부
   5. unrelated/ambiguous ancestor 및 durable rework/reviewer provenance
      누락 거부
   6. pending root의 comments/runs text-source lookup failure가 incomplete
      PR evidence로 수렴하지 않고 graph/event를 보존하는 fail-closed 경로
   7. fresh-GitHub interleaving에서 late ancestor activation / late active
      parent insertion 시 graph/event 보존
   8. cyclic link bounded refusal + diamond/shared-ancestor traversal
3. `docs/EDGE_REWORK_LIFECYCLE.md`: terminal convergence contract 추가

## 4. Explicit non-goals

- Hermes core / Kanban schema 변경 없음
- manual DB repair, forced promotion, `complete`-as-merge-proof, polling
  fallback, n8n schedule, second state owner 없음
- generic dependency bypass / forced promotion /
  completion-from-local-comment 없음
- H4V3-DJ source/runtime, PR #144 변경, #88 implementation/review replay
  없음
- merge / auto-merge / deploy 없음

## 5. Implementation requirements

- `origin/main` fetch 후 authoritative base SHA 기록. dedicated
  worktree(`.worktrees/t_c190a1ba`, branch
  `fix/issue-73-terminal-merge-convergence`) 사용.
- 기존 gate/transition contract 우회 금지: 본 pass는 pending gate shape만
  처리하고 모든 비수렴 shape은 classic lane 유지.
- durable evidence: convergence는 `github_pr_sync` event
  (`reason: terminal_merge_convergence`, merged-PR provenance,
  `merge_authority: human`, `auto_merge: false`)로 기록.
- mutable ancestor provenance: `blocked` node는 exact Issue/PR/head와
  `rework_round`가 있는 edge-owned `github_pr_rework`(또는 retry) event와
  canonical developer creation/parent evidence가 있어야 하며, allowed
  status node는 `kanban-reviewer` creation role과 stale rework parent
  evidence가 모두 있어야 한다. Status/title/body/Issue/PR 일치만으로는
  수렴하지 않는다.
- pending comments/runs lookup failure는 body-only fallback을 금지하고
  `text_source_lookup_failed`로 graph/event를 보존한다.
- fresh GitHub evidence 이후 `BEGIN IMMEDIATE` write transaction 안에서
  reachable node/edge closure, status/ownership, cycle/dangling/edge-set
  drift를 재검증하고, drift 시 write/event를 남기지 않는다.
- root write는 최종 direct-parent terminal predicate를 다시 평가한다.
- stale `blocked` convergence는 governing rework round 이후의 canonical
  `blocked` human hold 또는 matching current-round
  `github_pr_rework_attention` event가 있으면 fail-closed한다. 기존
  `block_kind`만으로 stale identity를 추론하지 않으며, hold rejection은
  node/root/event를 변경하지 않는다.
- ancestor walk는 active-path cycle detection을 사용하며 diamond/shared
  ancestor traversal은 허용한다.

## 6. Validation contract

`NOT RUN != PASS`. 실제 실행한 command만 PASS로 주장한다.

### EDGE_REWORK

```bash
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-terminal-convergence.py
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-rework.py
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-dependency-gate.py
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-completion.py
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-parking-comment.py
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-race-gate.py
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-head-binding-feedback.py
```

### STATIC_UNIT

```bash
python3 -m py_compile edge/kanban-github-sync.py
python3 -m py_compile edge/test-kanban-github-sync-terminal-convergence.py
PYTHONPATH=/ws/hermes-agent \
  /home/hermes/.hermes/profiles/kanban-main/lsp/node_modules/.bin/pyright \
  edge/kanban-github-sync.py edge/test-kanban-github-sync-terminal-convergence.py
git diff --check
```

## 7. Host/operator acceptance handoff

### Operator acceptance

**Copy-paste command**

```bash
# live edge dry-run(읽기 전용): #88 root 수렴 예측 확인
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  <deployed-core>/kanban-github-sync.py --board <board> <root-task-id> --dry-run --json
```

**PASS conditions**

- [ ] PR 머지 후 deploy된 edge의 dry-run이
      `terminal_merge_convergence_predicted` (수렴 시)를 반환하고,
      provenance/lookup 실패 시 `terminal_convergence_node_unconvergeable`
      또는 `text_source_lookup_failed`를 반환하며 graph/event를 보존
- [ ] live #88 그래프가 수렴되면 `t_fbd3f0b9 -> done`,
      `t_9a6395f0 -> archived`, `t_7e4e4523 -> done`

**If validation fails**

```bash
# edge 로그 + board 상태만 확인(mutation 금지)
hermes kanban show <task-id>
```

**After PASS**

- Merge-ready: YES(PR 머지 + deploy 후)
- Additional human judgment required: YES(deploy 결정)

## 9. Understanding handoff

- Confirmed cause: dependency gate가 GitHub fresh merge evidence보다
  먼저 평가되어 stale rework topology를 영구 pending으로 유지.
- Before flow: `root(todo) + pending child -> internal_dependency_pending
  (forever)`
- After flow: `root(todo) + stale chain + closed Issue + merged PR
  -> done/archived/done (1 pass, durable event, idempotent)`
- Canonical state/source owner: edge reconciliation
  (`edge/kanban-github-sync.py`)
- Ownership preserved: gate는 활성 작업 보호 유지, second state owner 없음
- Key design decision: 수렴은 **fresh GitHub authority**(closed Issue +
  all PR merged)에만 허용. DB-local shape pre-filter만으로는 never
  converge.
- Rejected alternative: generic dependency bypass(거부 — active work
  우회 위험), `complete`-as-merge-proof(거부 — merge 권위 훼손)
- First debugging entry point:
  `edge/kanban-github-sync.py::_attempt_terminal_merge_convergence`

## 10. Completion criteria

- [x] Source Issue / Kanban / idempotency provenance recorded
- [x] Latest fetched `origin/main` used as base
- [x] Dedicated branch/worktree used
- [x] Objective completed; non-goals respected
- [x] Correct canonical route selected
- [x] Ownership preserved (edge-only)
- [x] Security/auth/secret/network/policy impact gate evaluated
- [x] Required local validation executed truthfully (`NOT RUN != PASS`)
- [x] Work branch pushed and exactly one Korean PR opened/updated
- [x] Remaining HUMAN/HOST/BLOCKED state explicit
- [x] PR not merged without human/user authorization

## 11. Final report

### Summary

- Implemented: `edge/kanban-github-sync.py` terminal merge convergence
  pass + deterministic tests + lifecycle doc update.
- Intentionally not implemented: generic bypass, polling, deploy,
  H4V3-DJ-side changes.

### Provenance

- Source issue: `rhgo1749/hermes-n8n-control-plane#73`
- Kanban task: `t_c190a1ba`
- Idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:73`
- Request path: `.agent/pr-requests/REQ-073-merged-stale-rework-terminal-convergence.md`
- Lead/delegated workers: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

### Repository findings

- Selected route: `docs/EDGE_REWORK_LIFECYCLE.md` +
  `edge/kanban-github-sync.py`
- Canonical owners: edge reconciliation
- Request assumptions differing from repository/runtime evidence: none

### Cross-cutting impact

- Security: NONE / Auth: NONE / Host/network: NONE / Platform policy: NONE
- Follow-up / residual risk: deploy 후 live #88 그래프 수렴 확인
  (operator acceptance).

### Files changed

- `edge/kanban-github-sync.py`: terminal merge convergence pass plus the
  current-round human/operator hold predicate
- `edge/test-kanban-github-sync-terminal-convergence.py`: deterministic
  convergence, race, and later human-block/attention preservation regressions
- `docs/EDGE_REWORK_LIFECYCLE.md`: terminal convergence contract
- `.agent/pr-requests/REQ-073-merged-stale-rework-terminal-convergence.md`:
  this request

### Validation

| Validation | Result | Notes |
|---|---|---|
| Static/unit (py_compile + exact-file Pyright + diff-check) | PASS | head `8cfa929e2d7c98f0dc503d4d235d8e5481346e3e`; Pyright 0 errors/warnings/informations |
| n8n validation | NOT RUN | n8n topology out of scope |
| Edge terminal convergence | PASS | 84 passed, 0 failed; sabotage without hold predicate: exit 1, 76 passed, 8 failed |
| Edge rework/head-binding | PASS | 738 passed, 0 failed each |
| Dependency/completion/race gates | PASS | all named regression markers PASS |
| Parking-comment suite | PASS | 20 passed, 0 failed |
| Hermes plugin | NOT RUN | out of scope |
| Host dashboard/network | NOT RUN | operator acceptance pending |
| Telegram E2E | NOT RUN | out of scope |

### Remaining risks / owner

- deploy + live #88 convergence 확인: human/operator.

### Git / PR

- PR base SHA: `7e3262816da0202b924c3356e10f23daa8f5eb88`
- Fetched `origin/main` validation SHA: `a9aadafa48617ffa33df1f3702d7a2d4ac4a7d36`
- Worktree branch: `wt/t_2fb0b688` (tracking `origin/fix/issue-73-terminal-merge-convergence`)
- PR branch: `fix/issue-73-terminal-merge-convergence`
- Commit: `8cfa929e2d7c98f0dc503d4d235d8e5481346e3e`
- PR number/title/URL: PR #74 — https://github.com/rhgo1749/hermes-n8n-control-plane/pull/74 (Korean title/body, `Closes #73`)
- Working tree: clean after commit
- PR REST read-back: OPEN, non-draft, base `main`, exact head verified, four-file allowlist verified
- GitHub Actions: NOT RUN by repository policy
- Merge performed: NO
