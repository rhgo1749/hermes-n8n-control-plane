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

`hermes-agent` topic으로 opt-in 되었지만 live Hermes에 canonical checkout/Kanban board가 아직 없는 저장소가 `full` fallback에서 누락되는 회귀를 수정한다. 동시에 기존 registry-ready 저장소가 shared checkout의 일시적 HEAD 위치/개발 worktree 상태 때문에 event/full issue intake에서 탈락하지 않도록 intake trust boundary를 분리한다.

### Full fallback

`full` wake 자체의 historical full-scan semantics는 보존한다. Entry-point overlay는 registry에서 canonical checkout이 실제로 `missing`인 repository만 선별해 canonical core의 기존 `_provision_scoped_checkouts()` safe onboarding 경로로 먼저 등록한다. 이후 동일한 `full` scope가 core의 기존 registry/full-intake 경로를 계속 실행한다.

### Existing registry-ready repositories

이미 registry가 `ready=true`, `checkout_status=verified`로 관리하는 repository는 event/full automation에서 신규 checkout onboarding의 strict `HEAD == fresh GitHub default branch` 조건을 다시 적용하지 않는다. 대신 fresh GitHub metadata/topic/owner/default-branch opt-in은 계속 재검증한다. Explicit operator `--repository` probe는 기존 strict onboarding 계약을 유지한다.

### Provenance snapshot

Issue task provenance는 shared working-tree `HEAD`나 worktree cleanliness를 authority로 사용하지 않는다. 안전한 Git root/origin identity를 검증한 뒤, fresh GitHub default-branch SHA와 local `origin/<default>` commit SHA가 정확히 일치하는지 확인하고 그 immutable Git object에서 contract paths를 검증한다. Control-plane은 shared checkout을 fetch/reset/checkout하지 않는다.

이 분리는 다음 운영 상태를 허용하면서도 provenance freshness를 유지한다.

- shared main anchor가 current `origin/main`보다 뒤처진 상태
- 별도 worker/developer worktree를 가진 shared repository
- shared working tree의 non-authoritative untracked `.worktrees/*` 흔적

반대로 local `origin/<default>` 자체가 fresh GitHub SHA보다 stale하면 fail-closed한다.

Registry expansion/overlay 호출이 예외로 중단되면 이미 claim된 full scope를 durable requeue한다. 개별 missing repository의 bounded onboarding skip은 기존 ready repository의 full intake를 poison하지 않는다.

## Regression evidence

첫 host acceptance에서 모든 managed repository를 event-style strict onboarding scope로 승격한 구현은 신규 `hermes-github-kanban` onboarding에는 성공했지만 기존 `ctrl-hangul#100` / `re-bound#124`를 intake하지 못했다.

Read-only host probe에서:

- `ctrl-hangul`: fresh GitHub/main 및 local `origin/main` = `bda10368df5006800b6f4f6b5d1a57a4f35f2abf`; shared checkout `HEAD` = `88de189b8a56d3ef621c426b9e68b9c4d4fd6e49`; `HEAD` is ancestor of `origin/main`; working tree clean.
- `re-bound`: fresh GitHub/main 및 local `origin/main` = `1d58b7f1f8d0aab77821d4e7b9d2640564c5478a`; shared checkout `HEAD` = `42798c49fcdedc77e65d5db9901a6b511aa6a20f`; HEAD/origin are divergent; `.worktrees/*` untracked entries are present.
- both targeted strict onboarding probes returned `checkout_default_branch_mismatch`.
- neither `github:rhgo1749/ctrl-hangul:issue:100` nor `github:rhgo1749/re-bound:issue:124` existed in the corresponding board DB.

따라서 existing managed repository의 shared working-tree HEAD를 신규 onboarding/fresh provenance authority로 쓰는 것은 자동 intake와 shared-worktree isolation 계약을 동시에 깨뜨린다.

## Non-goals

- existing shared checkout fetch/reset/checkout 또는 branch 강제 최신화
- clone/materialization 구현 복제 또는 안전 경계 완화
- queue self-wake/liveness redesign
- H4V3-Meowcore legacy checkout-path migration
- n8n lifecycle 변경
- merge/auto-merge

## Validation

- `tests/test_full_fallback_onboarding_entrypoint.py`
  - missing checkout만 full provisioning 대상으로 선택
  - full scope mode/id/claim token 보존
  - existing ready event repository는 strict checkout HEAD gate 없이 fresh metadata 재검증 후 registry checkout 재사용
  - missing event repository는 기존 strict onboarding 유지
  - explicit manual repository probe는 strict onboarding 유지
  - origin snapshot은 shared HEAD drift를 허용하되 local origin ref가 fresh GitHub SHA보다 stale하면 fail-closed
  - one missing-repo skip이 existing full sweep를 poison하지 않음
  - registry failure durable requeue
  - run-context dry-run/manual propagation
- `tests/test_intake_completion_contract_entrypoint.py`
- `tests/test_repo_scoped_intake.py`
- `tests/test_repository_onboarding.py`
- `automation/n8n/scripts/validate.py`
- corrected host/Docker acceptance (required before merge):
  - exact branch/head checkout
  - live wrapper deploy + candidate/live SHA equality
  - one fresh full fallback ACK + upstream 200
  - `hermes-github-kanban` 신규 checkout/board onboarding 보존
  - existing board `ctrl-hangul#100` 및 `re-bound#124` idempotency keys가 실제 task로 생성됨
  - CtrlHangul/Re-Bound shared checkout HEAD/branch/status가 control-plane에 의해 변경되지 않음

The earlier acceptance at `5c72719bbd80958ca0f29be197950c8a6d2c60dc` is partial evidence only and is superseded by the corrected implementation. It proved missing-repository clone/board bootstrap, but failed the existing-ready Issue intake gate and therefore is not merge acceptance.
