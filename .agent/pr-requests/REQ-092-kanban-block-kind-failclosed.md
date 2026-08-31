# REQ-092: kanban_block kind 누락의 human-attention 오분류 방지 (fail-closed gate)

- Status: Draft
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `fix/issue92-kanban-block-kind-failclosed`
- Source-of-truth base: latest fetched `origin/main` (d0e8ef3, 2026-08-31)
- Remote delivery: Required
- PR title/body/final report language: Korean
- Request path: `.agent/pr-requests/REQ-092-kanban-block-kind-failclosed.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#92`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/92
- Kanban task ID: t_37dc5794 (intake root)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:92`
- Planning/lead owner: kanban-main
- Implementation owner: kanban-developer
- Automation stop state: `HUMAN_VALIDATION_REQUIRED` (live host hook wiring = human gate)

## 1. Objective

`kanban_block`의 `kind` 누락/`None` 호출이 durable human-attention(`blocked`) hold로
확정되는 것을 **control-plane pre_tool_call 게이트에서 fail-closed로 거부**하고,
`blocked` 상태의 canonical read surface(동기 context, dashboard)가 `block_kind`와
dependency provenance를 보존하도록 한다. Hermes core(`/ws/hermes-agent`)는 수정하지
않는다.

## 2. Confirmed background (read-only audit, 2026-08-31)

- Current behavior: core `tools/kanban_tools.py` kanban_block 핸들러는 `kind=None`을
  검증 통과시킴(유효 kind만 거부). `hermes_cli/kanban_db.py block_task()`는 `None`을
  "legacy un-typed"으로 처리 → human `blocked` 버킷으로 라우팅. `kind="dependency"`만
  `todo`(자동 승격)로 라우팅. 즉 dependency 대기 중 `kind` 누락 → human hold로 오분류.
- CLI: `hermes kanban block`는 `--kind` optional(default None, help "Omit for a
  generic block") — 같은 결함 경로.
- core `tools/kanban_tools.py:230` `_GOAL_MODE_BLOCK_ALLOWED_KINDS = {"dependency","needs_input"}`:
  goal_mode 태스크는 kind 미설정 통과 (별도 경위, 게이트 범위 밖).
- 본 저장소에는 `kanban_block` 호출자 0건(소비만: `edge/kanban-github-sync.py`
  block_kind 참조, `hermes-plugin/h4v3-overview/dashboard/plugin_api.py`
  `_needs_from_task`는 `blocked + block_kind in {needs_input,capability}`만 Need You).
- `block_kind`는 `tasks` 테이블 컬럼; `block_recurrences` 함께 저장. `task_links`
  (parent_id→child_id)가 durable dependency source of truth. `edge/kanban-github-sync.py`
  `_parent_dependency`(~1899)가 pending parents 조회 패턴을 보여줌.
- Enforcement 패턴: `~/.hermes/scripts/` `pre_tool_call` hook
  (`kanban-dedup-guard.py`, `kanban-workspace-guard.py`) — stdin JSON
  `{"tool_name","tool_input"}`, block 시 `{"action":"block","message":...}` + nonzero.
  config.yaml `hooks.pre_tool_call`에 matcher(`kanban_create`/`terminal`)로 등록.
  `hooks_auto_accept: true`이므로 block만 승인 대상.
- root cause(core `kind` required enum 전환)는 Hermes core 수정이 필요 → **본 REQ 범위
  밖**, human authority + core-repo follow-up.

### Control-plane ownership gate

- Ownership impact: AFFECTED — kanban_block 호출 경로에 control-plane deterministic
  게이트를 추가. core 시맨틱(종류별 라우팅)은 보존.
- New state/store: NO (기존 `tasks.block_kind`, `task_links`만 읽음)
- Existing authority bypassed: NO (게이트는 mutation 전 거부만; unblock/dependency
  auto-promotion/edge sync 미변경)

## 3. In scope

1. **fail-closed gate** (`~/.hermes/scripts/` 패턴의 repo-tracked 소스 + deploy):
   `kanban_block` MCP tool 및 `hermes kanban block` CLI 대상.
   - `kind` 누락/`None`/빈 문자열 → **거부**: durable task 상태 불변 + actionable
     validation failure.
   - 거부 diagnostics: 해당 task의 `task_links`에서 비종결(pending) parents가 있으면
     "unresolved dependencies: <parent_id>(<status>)... — use explicit
     kind=dependency" 포함; 없으면 "pick an explicit kind: dependency/needs_input/
     capability/transient".
   - explicit kind(`dependency`, `needs_input`, `capability`, `transient`)는 **통과**
     (기존 core 시맨틱 그대로 — dependency auto-promotion 회귀 없음).
   - goal_mode 태스크는 core가 이미 kind 제한(allowed: dependency/needs_input)을
     두므로 gate는 explicit kind 통과만 보장하면 됨 (core 이중 검증과 충돌 X).
   - board DB 미해결/읽기 실패 → fail-closed 거부 (dedup-guard 패턴과 동일 방향).
   - `kanban_block`이 아닌 tool → fail-open.
2. **read surface 보존** (`edge/kanban-github-sync.py` sync context +
   `hermes-plugin/h4v3-overview/dashboard/plugin_api.py`):
   `status=blocked` 태스크의 machine-readable `block:` 섹션/필드에
   `block_kind`(미설정 시 `untyped` 명시), pending parent id 목록,
   dependency-driven 여부(`auto_promotable: true/false`)를 포함. 기존
   `blocked/outcome=blocked`만 노출하던 lossy 경로에 block_kind와 provenance를
   추가. 기존 `_task_block_reason`, dashboard `_needs_from_task` 동작은 보존.
   **신규 state store 금지** — 기존 DB projection만.
3. **deterministic regression** (Issue #92 회귀 테스트 7건全覆盖):
   1) unresolved dep + kind 누락 → mutation 없음 + 검증 실패
   2) unresolved dep + explicit `dependency` → 기존 dependency-block 시맨틱 유지,
      dep 해소 후 기존 자동 승격 경로 정상
   3) unresolved dep + explicit human-attention kind → 허용되되 omitted-kind와
      구분(읽어보면 kind가 명시적으로 저장됨)
   4) dependency 없는 task + kind 누락 → silent human-attention default 아님,
      fail-closed
   5) read-back surface: dependency block과 human-attention block이 동일한
      `blocked` 정보로 붕괴하지 않음(block_kind/provenance 분별)
   6) 기존 `kanban_block(kind=...)` callers 및 dependency auto-promotion 회귀 없음
   7) validation 실패 시 task status/block metadata/event 부등분 mutation 없음
   isolated temp DB fixture 사용; 게이트는 실제 `block_task()` 경로와 같은 DB
   스키마로 검증.
4. **contract 문서** 업데이트: `edge/kanban-github-sync.py` docstring/
   `docs/EDGE_REWORK_LIFECYCLE.md`(block 시맨틱 섹션) + docs index에 해당
   gate/읽기 표면 contract를 반영. machine-local 경로는 문서에 기록하지 않는다.
5. **deploy**: `~/.hermes/scripts/` 대상 deployer(기존
   `deploy-intake-edge.sh` 계열 패턴)에 gate 소스 복사 + config.yaml
   `hooks.pre_tool_call`에 matcher `kanban_block` + `terminal` 등록. **live host
   적용은 HUMAN_VALIDATION_REQUIRED** — dev는 deploy dry-run + 검증만, 실제
   `~/.hermes/config.yaml` 변경은 human gate.

## 4. Explicit non-goals

- Hermes core(`/ws/hermes-agent`) 수정 금지 — `kind` required enum 전환은 follow-up.
- core `block_task` 라우팅 시맨틱 변경 금지.
- goal_mode 태스크의 core 이중 검증 수정 금지.
- model prompt에 사교 사례 하드코딩 금지.
- block reason 추론/허리스팅 금지 (kind만 gate).
- 신규 state store/DB 스키마 변경 금지.
- unblock/edge sync/dependency auto-promotion 동작 변경 금지.
- GitHub Actions 사용 금지 (로컬 검증만).
- PR merge/auto-merge 금지 (human authority).

## 5. Validation

- 새 게이트/리드서면 regression 파일: isolated temp board DB + 실제 스키마로
  `python3 edge/test-kanban-block-kind-failclosed.py`(또는 tests/ 컨벤션) 통과.
  pre-fix bite proof: gate 부재(또는 이전 동작)에서 케이스 1이 통과(=결함 재현) →
  gate 적용 후 실패로 전환.
- 기존 suite: README Runtime validation 목록(관련: `test_h4v3_overview.py`,
  `test_h4v3_notification_policy.py`, `edge/test-kanban-github-sync-*.py`) 재실행.
- `git diff --check` 통과, touched files Ruff/pyflakes 클린.
- PR body: `Closes #92.` (plain text, backtick/fence 밖, 번호 뒤 마침표) +
  GraphQL `PullRequest.closingIssuesReferences` fresh-read 확인. REST PATCH로만
  body 갱신.

## 6. Handoff contract

- dev: 구현 + 위 검증 + branch push + PR 생성(`Closes #92.` 포함) + exact head SHA
  + closing reference fresh-read. `kanban_request_review`으로 handoff.
- reviewer: exact head 검증 + 7건 회귀 fresh-run + PASS/REWORK.
- root close-out: specialist graph terminal + PR OPEN 상태에서 root core
  `kanban_complete`(provisional). edge가 review parking, human merge 이후
  authoritative done.
