# REQ-030: AGENT_REWORK_COMPLETE 마커 형식 오류 재발 방지 — PR attention 피드백 코멘트 + 형식 진단

- Status: Implementation complete / Review pending
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT` / `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `fix/edge-marker-feedback-issue30`
- Source-of-truth base: `origin/main` at `aaf87f92c8f2242a692ead241f2831d1288870c3` (fetched 2026-08-18)
- Remote delivery: Required; single PR against `main`
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-030-marker-feedback.md`
- Merge authority: Human/user only (user explicitly authorized merge in chat)
- Source issue: `rhgo1749/hermes-n8n-control-plane#30`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/30
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-main` (user-directed direct execution in chat; bounded patch with self-contained suite)
- Automation stop state: `HOST_VALIDATION_REQUIRED` (deploy to live runtime `~/.hermes/scripts/` after merge)

## Objective

ctrl-hangul PR #74 round-12의 완료 코멘트가 `AGENT_REWORK_COMPLETE (round 12, exact head …)` 형태(키=값 라인 없음)로 게시되어 edge의 strict 마커 파서에 거부됐지만, PR에는 어떤 피드백도 게시되지 않아(문서는 `HERMES_KANBAN_REWORK_ATTENTION` 코멘트를 명시하나 코드는 게시하지 않음) delivery 미기록 + attention hold가 이틀간 조용히 방치된 사건의 재발을 막는다.

1. attention 기록 시 PR에 idempotent machine-readable 피드백 코멘트(`HERMES_KANBAN_REWORK_ATTENTION` + task= + reason=)를 게시하고, body에 정확한 `AGENT_REWORK_COMPLETE` 재생성 템플릿과 `AGENT_REWORK_RETRY` 안내를 포함한다.
2. 마커 문자열은 있으나 구조(키=값)가 잘못된 완료 코멘트를 `completion_marker_malformed` 진단으로 구분하고 누락 필드를 evidence에 기록한다.
3. `docs/EDGE_REWORK_LIFECYCLE.md`에 라벨 의미 표(Issue/PR scope 포함)와 작업자용 마커 사전 점검 체크리스트, PR 피드백 코멘트 동작을 문서화한다. (부수로 5개 hermes-agent 토픽 저장소의 빈 에이전트 라벨 설명도 채웠으나 이는 repo metadata 변경으로 PR 범위 밖)
4. 기존 fail-closed 계약(자동 재시도 금지, 라벨 self-heal 복원만, 상태 전이 없음, dry-run 무변경)은 그대로 유지한다.

## Confirmed background

- Current behavior: `_record_rework_attention`은 Kanban task_comments/events에만 기록하고 PR 코멘트는 게시하지 않음. `_completion_marker`는 필드 미충족 시 `None`만 반환해 "마커 없음"(completion_handoff_missing)과 "마커 형식 오류"를 구분하지 못함.
- Defect: round-12 코멘트가 strict 파싱 실패 → `completion_handoff_missing`으로 처리됐고, PR에는 아무 안내가 없어 작업자/리뷰어가 거부 사유를 알 수 없었음. kanban-main의 검증 루프(19회)도 head/MERGEABLE만 확인해 delivery 이벤트 부재를 놓침.
- Affected operators/systems: edge sync, rework lifecycle, GitHub PR surface, Kanban blocked(needs_input) hold.
- Repository evidence: `edge/kanban-github-sync.py` `_completion_marker`(요구: task=/head=<40hex>/validation=passed), `_record_rework_attention`(2330–2362: task_comments만 기록), ctrl-hangul PR #74 이벤트 로그(round-12 delivery 이벤트 부재).
- Runtime/host evidence: 라이브 `/home/hermes/.hermes/scripts/kanban-github-sync.py`(mtime 2026-08-18)에도 PR attention 코멘트 게시 코드 없음; ctrl-hangul PR #74에 attention 코멘트 부재 확인(GitHub read).
- Source Issue-only product intent: Issue #30 요구사항 그대로.
- Assumptions: PR 코멘트 게시는 edge의 기존 GitHub 쓰기 권한 범위(라이프사이클 라벨/코멘트) 안.

### Control-plane ownership gate

- Ownership impact: NONE (edge가 계속 GitHub ↔ Kanban reconciliation owner; 새 state/store 없음)
- New state/store introduced: NO
- Existing authority bypassed: NO
- Required owner/document updates: `docs/EDGE_REWORK_LIFECYCLE.md` (동일 PR에서 갱신)

### Security / auth / secret / policy impact gate

- Security impact: NONE (신규 secret/auth 추가 없음; trusted-actor 기준 그대로)
- Auth/permission/secret boundary: NONE
- Host/network exposure: NONE
- External API/platform policy impact: NONE (기존 REST 경로 재사용)
- GitHub-hosted Actions: 변경 없음 (정책상 비활성 유지)

## In scope

1. `edge/kanban-github-sync.py`: `_malformed_completion_marker` 헬퍼 추가(구조 파싱 실패 시 comment_id + 누락 필드 반환), `_rework_delivery_evidence`에 `completion_marker_malformed` 분기, `_post_rework_attention_pr_comment` 헬퍼 추가(task+reason별 1회, 템플릿 포함), attention 두 호출부(blocked 분기·retry 분기)에 연결(실패 시 hold 유지하며 print만).
2. `edge/test-kanban-github-sync-rework.py`: tests 102–105 (malformed 마커 → `completion_marker_malformed` + PR 피드백 1회, 반복 tick idempotency, 정상 마커 → attention 코멘트 없음 + review 전이, 마커 부재 → generic 피드백).
3. `docs/EDGE_REWORK_LIFECYCLE.md`: 라벨 의미 표, 작업자 사전 점검 체크리스트, PR attention 피드백 코멘트 섹션, 전이 테이블/검증 설명 갱신.

## Explicit non-goals

- Hermes core 수정/fork, label 정책 변경, 자동 재시도/자동 round 시작, GitHub Actions, 머지 자동화.
- 라벨 설명 변경은 repo metadata 작업으로 PR 범위 밖(별도 GitHub API로 적용 완료, 사후 검증).
- 기존 `_completion_marker` 로직 변경(보수적 — 실패 시 `None` 유지).
- round-12/PR #74 상태 수정(ctrl-hangul 별개 surface; 이 PR로는 안 건드림).

## Implementation requirements

- 최신 `origin/main`(aaf87f9) fetch 후 branch `fix/edge-marker-feedback-issue30`에서 작업. ✓
- 새 state/store 없이 기존 edge 경로만 보강. ✓
- 배포: 머지 후 `automation/hermes/scripts/deploy-intake-edge.sh --hermes-home /home/hermes/.hermes` (backup → validate → atomic mv)로 라이브 반영. 롤백 = 출력된 backup 복원.
- secret/token tracked 파일 포함 없음.

## Validation contract and evidence

- `EDGE_REWORK`: `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py` — 실행 후 실측 수치 기록 (기존 + tests 102–105 신규 21 checks).
- `python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py` — PASS
- `python3 tests/test_repo_scoped_intake.py` — 영향 없음(변경 파일 무관)이나 스모크 실행 권장.
- `git diff --check` — PASS
- LSP: 래포 baseline pyright 진단과 비교해 신규 진단 0건.

## Delivery / stop state

- Branch: `fix/edge-marker-feedback-issue30`
- Implementation commits: `3aad2fa` (초기 구현), 이후 REQ/PR 기록 갱신 커밋
- PR: https://github.com/rhgo1749/hermes-n8n-control-plane/pull/31 (PR #31, base main, head `3aad2fa·이후`)
- Merge: 사용자가 chat에서 명시 승인(``머지``). 머지 수행 여부/시각을 PR body에 기록.
- Deploy: 머지 후 라이브 `~/.hermes/scripts/kanban-github-sync.py`에 `deploy-intake-edge.sh`로 적용 + SHA 검증.

## Rollback

배포 후 문제 시: deploy 스크립트가 출력한 timestamped backup(`.bak-*`)을 `mv`로 복원. git 롤백은 merge commit revert.

## Final report (PR body에 기록)

- Implemented: (1) PR attention 피드백 코멘트(게시 1회/task+reason), (2) `completion_marker_malformed` 진단 + 누락 필드, (3) 문서(라벨 표·체크리스트·동작) 갱신, (4) tests 102–105 추가.
- Intentionally not implemented: 라벨 정책 변경, 자동 재시도, Hermes core 변경.
- Files changed: `edge/kanban-github-sync.py`, `edge/test-kanban-github-sync-rework.py`, `docs/EDGE_REWORK_LIFECYCLE.md`, `.agent/pr-requests/REQ-030-marker-feedback.md`.
- Validation: EDGE_REWORK suite 실측 수치, py_compile, git diff --check.
- Operator acceptance: `deploy-intake-edge.sh --hermes-home /home/hermes/.hermes` 실행 + 설치 SHA == 머지 main SHA 확인.