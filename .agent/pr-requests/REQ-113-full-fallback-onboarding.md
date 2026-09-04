# REQ-113: full fallback 신규 저장소 onboarding hotfix

- Status: Implementation
- Project: `hermes-n8n-control-plane`
- Integration target: `main`
- Work branch: `issue113/full-fallback-onboarding-hotfix`
- Source base: `origin/main` `da0f83ba56cb5998f11f66a32ed4dc35e656a470`
- Source Issue: `rhgo1749/hermes-n8n-control-plane#113`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`, `HOST_NETWORKING`
- Merge authority: human/user only
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## Scope

`hermes-agent` topic으로 opt-in 되었지만 live Hermes에 canonical checkout/Kanban board가 아직 없는 저장소가 `full` fallback에서 누락되는 회귀를 수정한다.

Live intake entrypoint에서 claimed `full` wake를 registry가 발견한 모든 managed repository를 포함하는 bounded event-style scope로 승격한다. 그 결과 canonical core의 기존 `_provision_scoped_checkouts()` → registry reload → `_provision_bootstrap_boards()` → first `agent-ready` intake 경로를 그대로 재사용한다.

Registry expansion 실패 시 이미 claim한 full scope를 `registry_unavailable`로 requeue한 뒤 fail-closed한다. Event/manual scopes는 기존 동작을 유지한다.

## Non-goals

- clone/materialization 구현 복제 또는 안전 경계 완화
- queue self-wake/liveness redesign
- n8n lifecycle 변경
- merge/auto-merge

## Validation

- `tests/test_full_fallback_onboarding_entrypoint.py`
- `tests/test_repo_scoped_intake.py`
- `tests/test_repository_onboarding.py`
- `automation/n8n/scripts/validate.py`
- host: exact branch/head checkout, live script deploy, full fallback, checkout/board/task fresh-read
