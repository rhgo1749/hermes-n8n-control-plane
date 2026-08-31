# REQ-096: closed-unmerged PR을 명시적으로 supersede하고 agent-ready Issue를 새 round로 재투입

- Status: Draft
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `EDGE_REWORK`, `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `issue96/closed-unmerged-supersede-reintake`
- Source-of-truth base: latest fetched `origin/main` (d3992a5a)
- Remote delivery: Required
- PR title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-096-closed-unmerged-supersede-reintake.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#96`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/96
- Kanban task ID: t_0eb29732
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:96`
- Planning/lead owner: kanban-main
- Implementation owner: kanban-developer
- Automation stop state: `NONE`

## 0. Mandatory repository route

1. `AGENTS.md`
2. `README.md`
3. `docs/EDGE_REWORK_LIFECYCLE.md` (edge rework/label contract)
4. `docs/GITHUB_COMPLETION_LIFECYCLE.md` (completion projection)
5. `edge/kanban-github-sync.py` (evaluate_completion, BLOCKED reconciliation, trusted-signal machinery)
6. `edge/kanban_retry_signal_guard.py` (strict whole-comment trusted-signal guard)

## 1. Objective

maintainer가 closed-unmerged PR을 명시적으로 폐기(superseded/abandoned)하고 source Issue를 새
agent round로 되돌릴 수 있는 canonical edge transition을 `edge/kanban-github-sync.py`에 추가한다.
단순 PR close만으로는 절대 새 round를 시작하지 않음(자동 추론 금지).

## 2. Confirmed background

- Current behavior: `evaluate_completion()`은 closed-unmerged effective PR를
  `linked_pr_closed_not_merged`로 REVIEW/BLOCKED에 fail-closed 유지한다.
- Defect/limitation: no-PR BLOCKED→READY resume은 `decision.pull_requests`가 비어 있어야
  하므로, 과거 closed linked PR이 남아 있으면 일반 `agent-ready` intake로 복귀하지 못한다.
- Repository evidence:
  - `evaluate_completion()` (edge/kanban-github-sync.py:707) — `linked_pr_closed_not_merged` 경로.
  - `_superseded_closed_pr_numbers()` (:670) — same-head newer-merge 기반 자동 supersession.
  - `evaluate_blocked_resume()` (:1396) — no-PR BLOCKED→READY resume.
  - `TRUSTED_GITHUB_ACTORS` (:119), `REWORK_RETRY_MARKER` (:133), `REWORK_COMPLETE_MARKER` (:124).
  - `edge/kanban_retry_signal_guard.py` — 전체-comment machine-readable trusted-signal guard 패턴.
- Source Issue-only product intent: one-shot, trusted-maintainer-signal-only supersede.
- Assumptions: trusted actor set(`rhgo1749`) 재사용. supersede signal은 PR-scope machine-readable
  comment로 표현(implementation 재검토 가능 — 아래 계약만 충족하면 됨).

### Control-plane ownership gate

- Ownership impact: AFFECTED (edge에만 추가 transition; GitHub=human surface 유지)
- New state/store introduced: NO (durable one-shot consumed-signal 기록만 — 기존 event/label 경계 재사용)
- Existing authority bypassed: NO
- Required owner/document updates: `docs/EDGE_REWORK_LIFECYCLE.md` 또는 `docs/GITHUB_COMPLETION_LIFECYCLE.md`에
  supersede transition 계약 추가 (durable contract 변경 시).

### Security / auth / secret / policy impact gate

- Security impact: AFFECTED (trusted actor + one-shot signal 검증 — fail-closed 필수)
- Auth/permission/secret boundary: trusted GitHub actor gate 재사용, secret 없음
- Host/network exposure: NONE
- External API/platform policy: NONE
- GitHub-hosted Actions enable/re-enable: NO (로컬 검증만)

## 3. In scope

1. trusted maintainer signal로 특정 PR를 `superseded/abandoned` 처리하는 edge transition.
2. fresh read에서 source Issue가 OPEN + `agent-ready`인지 재검증.
3. 대상 PR이 CLOSED + NOT MERGED인지 재검증.
4. open linked PR 또는 다른 ambiguous rework round 존재 시 fail-closed.
5. consumed signal은 one-shot으로 기록해 반복 dispatch 방지.
6. 폐기된 PR은 이후 completion evaluator에서 명시적 superseded evidence로 제외.
7. Issue를 READY로 되돌려 새 implementation round 허용.
8. 자동 inference 금지: 단순 PR close만으로는 절대 새 round 시작 금지.
9. 위 8가지 테스트 요구를 커버하는 deterministic regression(fixture).
10. durable contract 변경에 따른 canonical 문서 갱신.

## 4. Explicit non-goals

- Hermes core 수정/fork.
- 새 task DB / notification DB / parallel state store.
- second dispatcher 또는 second completion owner.
- n8n에서 worker/spawn/completion 재구현.
- Overview task mutation.
- GitHub-hosted Actions enable/re-enable 또는 신규 hosted CI workflow.
- merge/auto-merge.
- 관련 없는 cleanup/redesign/migration.

## 5. Implementation requirements

- 최신 `origin/main` fetch 후 authoritative base SHA 기록.
- dedicated branch/worktree(`issue96/closed-unmerged-supersede-reintake`) 작업.
- 기존 owner/caller/state 경계 확인, 가장 작은 root-cause 변경 선호.
- secret/token/machine-local path를 tracked source/PR/chat에 금지.
- edge lifecycle은 fresh GitHub evidence와 기존 Kanban transition contract를 우회하지 않음.
- durable contract 변경 시 관련 canonical 문서를 같은 PR에서 갱신.

## 6. Validation contract

GitHub Actions는 비활성화(저장소 정책) — 로컬 검증이 required. `NOT RUN != PASS`.

### `EDGE_REWORK`
```bash
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-rework.py
```
(상위 `HERMES_DELEGATED_CHILD_CONTEXT=1`으로 mutation guard가 차단되면 clean child-marker
environment에서 재실행한 사실 보고.)

### `STATIC_UNIT`
```bash
python3 -m py_compile <changed-python-files>
git diff --check
```
변경 표면에 필요한 edge regression 추가/실행.

### 테스트 요구 (Issue #96)
1. Issue OPEN + `agent-ready` + one closed-unmerged linked PR + trusted supersede signal -> READY 1회
2. 같은 signal 재사용 -> no-op
3. 단순 PR close, signal 없음 -> 기존 fail-closed 유지
4. Issue CLOSED -> 새 round 금지
5. open linked PR 존재 -> fail-closed
6. merged PR -> completion precedence 유지
7. multiple/ambiguous linked PR -> fail-closed
8. GitHub lookup 실패 -> no mutation

매 새 regression은 pre-fix bite proof(구현 전 FAIL → 구현 후 PASS) 필수.

## 7. Host/operator acceptance handoff

로컬 edge regression + static unit 검증만. runtime/dashboard/network/Telegram gate 없음.
`HOST_VALIDATION_REQUIRED` 아님.

## 8. Lead / delegation contract

- kanban-main: 이슈/컨텍스트/그래프/판정 (이 카드).
- kanban-developer: 구현 + 로컬 검증 + PR 생성/갱신 + 정확한 evidence.
- kanban-reviewer: current PR/head/diff/evidence 독립 검증 → PASS/REWORK.
- PR 생성/갱신은 agent, merge는 human 전용.

## 9. Understanding handoff

- Confirmed cause/need: closed-unmerged linked PR이 `linked_pr_closed_not_merged`로
  fail-closed 유지되며, no-PR resume은 linked PR가 비어야 해 새 round로 복귀 불가.
- Before flow: closed-unmerged PR + open agent-ready Issue -> REVIEW/BLOCKED에 고착.
- After flow: trusted one-shot supersede signal -> Issue OPEN+agent-ready & PR CLOSED+NOT MERGED
  재검증 -> consumed one-shot 기록 -> PR는 superseded evidence로 completion에서 제외 ->
  Issue READY로 새 round 허용. signal 없으면 기존 fail-closed 유지.
- Canonical state/source owner: edge(`edge/kanban-github-sync.py`).
- Callers/consumers: completion evaluator, BLOCKED reconciliation, rework ingress.
- Ownership preserved/changed: preserved (edge에만 추가).
- Key design decision: trusted actor + one-shot machine-readable signal, fail-closed, 자동 추론 금지.
- Rejected alternative and reason: bare PR close를 신호로 쓰는 자동 inference — 위임이라 거부.
- First debugging entry point: `evaluate_completion()` + `evaluate_blocked_resume()` + trusted-signal guard.

## 10. Completion criteria

- [ ] Source Issue / Kanban / idempotency provenance 기록
- [ ] Latest fetched `origin/main` base 사용
- [ ] dedicated branch/worktree 사용
- [ ] Objective 완료, non-goals 준수
- [ ] 8가지 테스트 요구 커버 + pre-fix bite proof
- [ ] required local validation truthfully 실행 (`NOT RUN != PASS`)
- [ ] durable contract 변경 시 canonical 문서 갱신
- [ ] work branch push + exactly one Korean PR(opened/updated)
- [ ] PR에 `Closes #96.` plain-text closing line (backtick/fence 밖) + GraphQL closingIssuesReferences 검증
- [ ] PR not merged without human/user authorization

## 11. Final report

(개발/리뷰 후 채움 — 템플릿 §11 형식 유지.)
