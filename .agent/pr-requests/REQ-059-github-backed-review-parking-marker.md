# REQ-058b: GitHub-backed review 파킹 마커 코멘트 + 상태 의미 문서화

- Status: Draft
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION` / `DOCS_ONLY`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `hermes-n8n-control-plane/t_0bbfcd74-edge-ux-github-backed-review`
- Source-of-truth base: latest fetched `origin/main` (b7624c0, PR #60 병합 직후)
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-059-github-backed-review-parking-marker.md`
- Merge authority: Human/user only
- Source issue: none (사용자 피드백 기반 direct edge UX 요청, 2026-08-23)
- Kanban task ID: t_0bbfcd74 (Hermes Kanban)
- Intake idempotency key: 해당 없음 (Kanban 발행 작업, GitHub Issue intake 아님)
- Planning/lead owner: kanban-developer
- Implementation owner: kanban-developer
- Automation stop state: `NONE`

## 0. Mandatory repository route

1. `AGENTS.md`
2. `README.md`
3. `docs/README.md` → 변경 표면의 canonical 문서로 `docs/GITHUB_COMPLETION_LIFECYCLE.md` 선택
4. `edge/kanban-github-sync.py` (`apply_decision`, `evaluate_completion`)
5. `edge/test-kanban-github-sync-rework.py`, `edge/test-kanban-github-sync-completion.py`

## 1. Objective

Edge가 GitHub-backed 카드를 done→review로 파킹할 때 보드만 봐도 "사람 행동 필요 여부"와 "다음 트리거"가 즉시 드러나도록 구조화된 파킹 마커 코멘트 1건을 같은 트랜잭션에 추가하고, 그 상태 의미(`review` = merge 대기 파킹)를 canonical 문서에 명시한다.

## 2. Confirmed background

- Current behavior: `evaluate_completion()`이 review desired(no_linked_pr / linked_pr_open / linked_pr_closed_not_merged / linked_pr_not_merged)를 반환하면 `apply_decision()`이 상태만 review로 바꾼다. 카드에는 이유·다음 트리거·행동 필요 여부가 전혀 남지 않아 사용자가 개입 필요성을 판단할 수 없었다.
- Evidence: t_ef56c2f2(ctrlhangul 보드) github_pr_sync done→review reason=no_linked_pr — 실제로는 자식 구현 태스크 진행 중으로 사람 행동 불필요한 상황이었음.
- Ownership impact: NONE — edge가 이미 소유한 GitHub↔Kanban reconciliation 내부의 관찰 가능성 추가. 새 state/store 없음(task_comments 기존 채널 재사용).

## 3. In scope

1. `edge/kanban-github-sync.py`: `apply_decision` DONE→REVIEW 경로에서 `_parking_comment_for_decision()` 렌더 + `_append_parking_comment_if_absent()`로 task_comments 1건 부착(전환·코멘트·github_pr_sync 이벤트 동일 트랜잭션).
   - `[parked: awaiting-merge] reason=linked_pr_open pr=#74 next=github-edge(merge 감지 시 자동 해제). 사람 행동 불필요.`
   - `[parked: awaiting-pr] reason=no_linked_pr next=github-edge(PR 링크 감지 시 자동 재평가). 사람 행동 불필요.`
   - 중복 금지: 동일 task+reason+pr 조합(정확히 동일 body)은 재부착하지 않음.
   - desired_status/판정 로직/fail-closed 규칙은 일절 변경 없음. 상태값은 review 그대로.
2. `docs/GITHUB_COMPLETION_LIFECYCLE.md`: "review = merge 대기 파킹" 의미 명시, blocked/request-review 등 사람 행동 상태와 구분표, 마커 형식 설명 추가.
3. `edge/test-kanban-github-sync-parking-comment.py`: 신규 회귀 테스트(마커 1회 부착, 반복 동기화 중복 없음, 상황 변화 시 1회 추가, done 전환/비권위/preserved 경로 무마커).

## 4. Explicit non-goals

- scheduled 투영 재설계(후속 감사 태스크 t_cf9130bc, READ-ONLY)
- Core Hermes kanban 상태 어휘 수정
- evaluate_completion 판정 로직/fail-closed 변경
- GitHub 코멘트 POST(카드 코멘트는 Kanban 내부에만 남김), 새 PR 자동 생성, merge/auto-merge

## 5. Validation contract (실제 실행 결과)

| Validation | Result | Notes |
|---|---|---|
| `python3 -m py_compile edge/kanban-github-sync.py` | PASS | |
| `python3 -m py_compile edge/test-kanban-github-sync-parking-comment.py` | PASS | |
| `git diff --check` | PASS | whitespace 오류 없음 |
| `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-parking-comment.py` | PASS | 20 passed, 0 failed (신규) |
| `... edge/test-kanban-github-sync-completion.py` | PASS | ALL COMPLETION REGRESSIONS PASS |
| `... edge/test-kanban-github-sync-rework.py` | PASS | 722 passed, 0 failed |
| GitHub Actions | NOT RUN | 저장소 정책상 비활성 — 실패 아님, 로컬 검증으로 대체 |

변경 범위 assertion: `git diff --name-only` + untracked = `edge/kanban-github-sync.py`(엣지 스크립트 1개), `docs/GITHUB_COMPLETION_LIFECYCLE.md`(문서 1개), `edge/test-kanban-github-sync-parking-comment.py`(신규 테스트 1개)뿐임.

## 6. Understanding handoff

- Before flow: done 카드 + open/unmerged PR → edge가 status=review로만 전환(무코멘트).
- After flow: 동일 전환 + 같은 트랜잭션에 `[parked: ...]` 코멘트 1건(중복 억제). merge 감지 시 REVIEW→DONE 자동 해제는 기존과 동일.
- Key design decision: 마커를 apply_decision 트랜잭션 안에 두어 전환·증거·UX 코멘트 원자성 유지; idempotency key는 렌더된 body 자체(조합당 1회).
- Rejected alternative: GitHub PR 코멘트 POST — 외부 side effect/레이트 한도/실패 처리 복잡도 증가, 카드 가독성 목적과 불일치.

## 7. Completion criteria

- [x] Source Issue/Kanban/idempotency provenance 기록
- [x] 최신 fetch된 origin/main(b7624c0) base
- [x] dedicated worktree branch 사용
- [x] Objective 완료, non-goals 준수
- [x] 소유권 경계 보존(edge 내부 관찰 가능성만 추가)
- [x] 필수 로컬 검증 전부 실행·PASS
- [ ] Work branch push + 한국어 PR 1건 OPEN(구현 단계에서 수행)
- [x] merge/auto-merge 없음(사람 전용)
