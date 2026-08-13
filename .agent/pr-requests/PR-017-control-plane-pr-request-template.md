# PR-017: control-plane 전용 PR 요청서 템플릿 추가

- Status: Review
- Project: `hermes-n8n-control-plane`
- Product type: `DOCS_ONLY`
- Validation profiles: `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `chore/pr-request-template`
- Source-of-truth base: latest fetched `origin/main`
- Remote delivery: Required
- Pull request language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/PR-017-control-plane-pr-request-template.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#17`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/17`
- Kanban task ID: none — 사용자가 `agent-ready`를 제거하고 직접 PR 작성을 요청함
- Intake idempotency key: not applicable
- Planning/implementation owner: direct human-assisted repository maintenance
- Automation stop state: `NONE`

## Objective

다른 H4V3 저장소의 Agentic PR Request 패턴을 참고해 이 control-plane 저장소에 repository-owned PR 요청서 계약을 추가하고, root `AGENTS.md`에서 발견 가능하게 한다.

## Confirmed background

- 기존 `AGENTS.md`는 GitHub-hosted Actions 비활성화와 local validation truthfulness만 정의했다.
- CtrlHangul/Re-Bound는 `.agent/PR_REQUEST_TEMPLATE.md` + `.agent/pr-requests/PR-NNN-<slug>.md` 패턴을 사용한다.
- 이 저장소는 GitHub / Hermes Kanban / edge / n8n / Overview / Telegram의 ownership 경계를 명시적으로 보존해야 한다.

## In scope

1. `.agent/PR_REQUEST_TEMPLATE.md` 추가
2. `.agent/pr-requests/` storage contract 추가
3. root `AGENTS.md`에 Issue-driven request routing 추가
4. 이번 bootstrap 변경의 PR-017 요청서 기록

## Explicit non-goals

- runtime/automation/lifecycle 코드 변경 없음
- Hermes core 수정 없음
- GitHub-hosted Actions enable/re-enable 없음
- 새 task/state/notification store 없음
- merge/auto-merge 없음

## Validation

- Changed files are documentation/policy only.
- GitHub compare/diff에서 runtime source 변경이 없는지 확인.
- `AGENTS.md`의 기존 CI policy가 유지되는지 확인.
- Markdown/contract 내용은 repository docs의 실제 route와 일치하는지 확인.
- GitHub-hosted Actions checks는 저장소 정책상 요구하지 않음.

## Understanding handoff

- Before: Issue-driven work에 repository-owned PR request contract가 없음.
- After: Issue → `.agent/PR_REQUEST_TEMPLATE.md` → task-specific `.agent/pr-requests/PR-NNN-<slug>.md` → branch/PR handoff.
- Ownership: 기존 GitHub/Kanban/edge/n8n/Overview/Telegram owner는 변경하지 않음.
- First debugging/review entry point: `AGENTS.md`와 `.agent/PR_REQUEST_TEMPLATE.md`.

## Completion criteria

- [x] control-plane 전용 template 추가
- [x] request storage path 계약 추가
- [x] root AGENTS routing 추가
- [x] human merge authority 유지
- [x] runtime/automation 동작 변경 없음
- [ ] human review
- [ ] merge
