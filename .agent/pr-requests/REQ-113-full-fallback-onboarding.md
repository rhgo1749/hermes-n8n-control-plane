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

`full` wake 자체의 historical full-scan semantics는 보존한다. Entry-point overlay는 registry에서 canonical checkout이 실제로 `missing`인 repository만 선별해 canonical core의 기존 `_provision_scoped_checkouts()` safe onboarding 경로로 먼저 등록한다. 이후 동일한 `full` scope가 core의 기존 registry/full-intake 경로를 계속 실행하므로 이미 ready인 repository는 신규 onboarding의 strict fresh-HEAD validator를 다시 통과할 필요 없이 기존 `agent-ready` Issue intake를 유지한다.

이 분리는 shared checkout이 clean하더라도 current `HEAD`가 fresh GitHub default-branch SHA와 정확히 일치하지 않을 수 있는 정상 운영 상태를 보호한다. Control-plane은 기존 shared checkout을 fetch/reset/checkout하여 강제로 최신화하지 않는다. 신규 missing checkout만 safe clone/atomic registration 대상으로 삼는다.

Registry expansion 자체가 실패하거나 overlay provisioning 호출이 예외로 중단되면 이미 claim된 full scope를 durable requeue하고 fail-closed한다. 개별 missing repository의 bounded onboarding skip은 기존 ready repository의 full intake를 poison하지 않는다. Ordinary event/manual scope는 기존 strict onboarding 동작을 유지한다.

## Regression evidence behind the correction

첫 host acceptance에서 모든 managed repository를 event-style scope로 승격한 구현은 신규 `hermes-github-kanban` onboarding에는 성공했지만 기존 `ctrl-hangul#100` / `re-bound#124`를 intake하지 못했다.

Read-only host probe에서:

- `ctrl-hangul`: fresh GitHub/main 및 local `origin/main` = `bda10368df5006800b6f4f6b5d1a57a4f35f2abf`, shared checkout `HEAD` = `88de189b8a56d3ef621c426b9e68b9c4d4fd6e49` (HEAD is an ancestor of origin/main)
- `re-bound`: fresh GitHub/main 및 local `origin/main` = `1d58b7f1f8d0aab77821d4e7b9d2640564c5478a`, shared checkout `HEAD` = `42798c49fcdedc77e65d5db9901a6b511aa6a20f` (divergent shared development anchor)
- both targeted strict onboarding probes returned `checkout_default_branch_mismatch`
- neither Issue idempotency key existed in its board DB

따라서 strict onboarding을 모든 existing ready checkout에 적용하는 것은 기존 full-scan 계약을 깨뜨린다. Corrected implementation은 missing checkout만 onboarding한다.

## Non-goals

- existing shared checkout fetch/reset/checkout 또는 branch 강제 최신화
- clone/materialization 구현 복제 또는 안전 경계 완화
- queue self-wake/liveness redesign
- H4V3-Meowcore legacy checkout-path migration
- n8n lifecycle 변경
- merge/auto-merge

## Validation

- `tests/test_full_fallback_onboarding_entrypoint.py`
  - missing checkout만 provisioning 대상으로 선택
  - ready checkout은 strict onboarding에서 제외
  - full scope mode/id/claim token 보존
  - one missing-repo skip이 existing full sweep를 poison하지 않음
  - dry-run propagation
  - event scope unchanged
  - registry failure durable requeue
- `tests/test_intake_completion_contract_entrypoint.py`
- `tests/test_repo_scoped_intake.py`
- `tests/test_repository_onboarding.py`
- `automation/n8n/scripts/validate.py`
- host/Docker acceptance:
  - exact branch/head checkout
  - live wrapper deploy + candidate/live SHA equality
  - one fresh full fallback ACK + upstream 200
  - `hermes-github-kanban` 신규 checkout/board onboarding 보존
  - existing board `ctrl-hangul#100` 및 `re-bound#124` idempotency keys가 실제 task로 생성됨
  - shared checkout HEAD/branch는 control-plane에 의해 변경되지 않음
