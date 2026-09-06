# REQ-127: missed agent-ready webhook low-frequency self-heal

- Status: Implementation
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`, `HOST_NETWORKING`
- Integration target branch: `main`
- Required work branch: `hotfix/127-missed-webhook-fallback`
- Source-of-truth base: `origin/main` `0bc8bb3aa5ac227662b5512d08933bdd8dd53d82`
- Remote delivery: Required
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#127`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:127`
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## Objective

GitHub webhook 1회 유실만으로 open `agent-ready` Issue가 Kanban 밖에 영구 고립되지 않도록, 기존 router durable scope queue와 canonical Hermes intake job을 재사용하는 저빈도 full-intake safety wake를 추가한다.

## Confirmed background

- `#104`는 `agent-ready` 상태였지만 board task가 없었고 targeted intake dry-run에서는 candidate로 정상 발견됐다.
- PR #114는 `full` fallback이 실행되면 existing/new managed repository의 agent-ready intake를 처리하도록 복구했다.
- 현재 durable contract는 normal webhook을 primary path로 쓰며 `/fallback` 자동 호출을 의도적으로 미구현 상태로 남겼다.
- `github-router`가 이미 canonical durable scope queue와 lease-controller wake를 소유한다.

### Ownership gate

- Ownership impact: NONE. lifecycle owner는 canonical intake/edge로 유지한다.
- New state/store introduced: NO.
- Existing authority bypassed: NO.
- n8n은 Schedule Trigger나 lifecycle write를 추가하지 않는다.
- periodic safety wake는 webhook reconciliation을 수행하지 않는다.

## In scope

1. `router_entrypoint.py`에서 기본 3600초, 최소 300초의 bounded safety interval을 제공한다.
2. 매 tick stable synthetic delivery identity로 `full` scope를 enqueue하고 기존 `_wake()`만 호출한다.
3. 동일 safety scope가 queued 상태면 재-wake하고, in-flight/pending이면 중복 wake를 생략한다.
4. startup 즉시 scan하지 않고 첫 interval 이후 실행한다.
5. focused regression과 canonical event/intake validation을 실행한다.
6. Ubuntu live router/edge 배포 후 #104 intake 및 runtime provenance를 read-back한다.

## Non-goals

- n8n polling/Schedule Trigger 부활
- periodic webhook reconciliation
- GitHub/Kanban lifecycle state 직접 판정·mutation
- 새 DB/dispatcher/completion owner
- agent-* label semantics 변경
- merge/auto-merge

## Validation contract

Repository-local:

- `python3 -m pytest -q tests/test_periodic_full_intake_fallback.py tests/test_github_router.py tests/test_github_router_startup.py tests/test_github_event_concurrency_contract.py`
- `python3 automation/n8n/scripts/validate.py`
- `python3 -m py_compile automation/n8n/github-router/router_entrypoint.py tests/test_periodic_full_intake_fallback.py`
- `git diff --check`

Host/runtime:

- exact branch/head checkout
- router candidate/live SHA equality
- loopback `/healthz` and authenticated `/debug/state`
- one bounded periodic/full-intake wake path proves upstream 2xx
- `github:rhgo1749/hermes-n8n-control-plane:issue:104` card read-back
- normal webhook path remains healthy
