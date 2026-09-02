# REQ-099: stale test contract governance

- Status: Ready
- Project: hermes-n8n-control-plane
- Source issue: rhgo1749/hermes-n8n-control-plane#99
- Required work branch: `hotfix/99-test-contract-governance`
- Source-of-truth base: latest fetched `origin/main`
- Validation profiles: DOCS_ONLY / STATIC_UNIT audit
- Kanban task ID: none — direct user-authorized hotfix
- Automation stop state: NONE
- Merge authority: user explicitly authorized merge for this hotfix

## Objective

기존 테스트 변경을 현재 canonical lifecycle/ownership 계약에 종속시키고, 현재 control-plane 정본과 충돌하는 stale test를 점검한다.

## Canonical route

- `AGENTS.md`
- `docs/EDGE_REWORK_LIFECYCLE.md`
- `edge/kanban-github-sync.py`
- 관련 `edge/test-kanban-github-sync-*.py`

## Contract

- 구현 충돌만으로 기존 테스트 수정/삭제/skip/assertion 완화 금지
- 기존 계약이 Issue/canonical contract로 supersede되었을 때만 테스트 변경
- 변경 이유, superseding evidence, replacement validation 기록
- 계약 변경 시 관련 기존 테스트 stale audit

## Non-goals

- agent-* lifecycle semantics 변경
- n8n/edge ownership 변경
- 테스트 수 축소를 위한 광범위 정리
- GitHub Actions 재활성화

## Validation

- 현재 `agent-ready`/`agent-blocked` Issue-side와 `agent-rework`/`agent-working`/`agent-review-ready` PR-side 정본을 기준으로 audit
- 실행 테스트 자체를 변경하지 않으면 runtime PASS를 주장하지 않음
