# REQ-169: intake wake liveness hotfix

- Status: Implementation
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT` / `N8N_VALIDATE` / `HOST_NETWORKING`
- Integration target branch: `main`
- Required work branch: `hotfix/169-intake-wake-liveness`
- Source-of-truth base: `5109902663ca5bdcfc3d17225248bb0b9b56ae7f`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-169-intake-wake-liveness.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#169`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/169`
- Kanban task ID: N/A — operator hotfix from live CtrlHangul #115 incident evidence
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:169`
- Planning/lead owner: ChatGPT operator session
- Implementation owner: ChatGPT operator session
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## 0. Mandatory repository route

`AGENTS.md` → `README.md` → `docs/README.md` → `docs/GITHUB_EVENT_CONCURRENCY.md` → router / lease-controller / actuator source → focused router/lease/fallback tests → `docs/OPERATIONS.md` for host acceptance.

## 1. Objective

Make accepted GitHub intake/rework work converge from durable state even when volatile wake execution is interrupted or the fixed actuator is busy, without adding a second lifecycle owner, scheduler, or state store.

## 2. Confirmed background

- CtrlHangul PR #115 received a fresh maintainer `agent-rework` on 2026-09-15, while a repository-scoped intake event was also arriving.
- Live router evidence showed the CtrlHangul durable scope still queued with `attempts=0`, no in-flight claim, an idle actuator, and a paused lease after another scope had completed.
- Current intake invocation claims exactly one queued scope. The first hotfix draft chained only work eligible at that instant; a sole retry scope with future `not_before` could therefore remain stranded until another event or the hourly safety wake.
- Lease `/trigger` persists `pending` and then relies on a daemon background thread; current startup has no resume for an accepted persisted `pending` trigger.
- Managed PR rework/merge uses the low-latency n8n → fixed edge actuator path. Intake and edge-sync share the actuator `_RUN_LOCK`; a busy failure is retryable externally but has no durable repository-scoped internal fallback.
- Exact historical thread/process loss cannot be proven from retained logs. The implementation targets the confirmed liveness gaps rather than claiming an unobserved failure line.

Ownership impact: `AFFECTED` only at the event-transport/liveness layer. Canonical edge remains GitHub↔Kanban lifecycle authority.
New state/store introduced: `NO`.
Existing authority bypassed: `NO`.
Security/auth/secret impact: `NONE`; existing loopback Bearer-authenticated boundaries are reused.
Host/network exposure: `NONE`.

## 3. In scope

1. After a fresh ACK/requeue/pending transition, derive the earliest durable queue `not_before`: wake immediately when due, otherwise install one coalesced one-shot wake for that deadline.
2. At router startup, recover the earliest already-durable queue deadline without creating an immediate full scan; future deadlines must be re-scheduled from durable state.
3. Resume the latest persisted lease-controller `pending` trigger on controller startup; do not replay active/paused/failed leases.
4. Boundedly retry the fixed actuator's `409` busy response inside the existing serialized lease trigger instead of immediately abandoning the accepted wake.
5. If a supported managed PR direct edge wake fails, preserve it as the existing repository-scoped durable intake scope and wake canonical intake; later reconciliation must fresh-read GitHub.
6. Add focused regression and update `docs/GITHUB_EVENT_CONCURRENCY.md`.

## 4. Explicit non-goals

- No Hermes core fork/change.
- No second dispatcher/completion owner.
- No new DB/state store or high-frequency polling loop.
- No n8n lifecycle state.
- No direct queue/state-file repair for the live incident.
- No webhook payload as authoritative lifecycle evidence.
- No merge/auto-merge.

## 5. Implementation requirements

- Preserve one-claim-per-intake-invocation isolation and existing claim fencing/backoff/pending semantics.
- A chained wake or delayed reservation happens only after the scope transition is durably committed. Queue read-back/wake failure must not roll back or falsify the committed transition.
- Delayed retry liveness is a volatile one-shot hint derived from the durable queue, not a polling scheduler or new state store. Coalesce future hints to the earliest in-process deadline; at firing time fresh-read the queue, no-op if the work was already consumed, and re-schedule only if the durable earliest `not_before` moved later.
- Startup queue recovery is bounded, reads the earliest durable `not_before`, and must not enqueue a full scope. An eligible deadline wakes immediately; a future deadline installs the same one-shot so process restart reconstructs a lost volatile timer from durable state.
- Pending-lease recovery reuses the fixed actuator and existing `_TRIGGER_LOCK`; persisted non-pending lease states are not replayed.
- Actuator `409` contention recovery must be finite and serialized. Exhaustion remains an explicit failed lease, not an infinite retry loop.
- PR durable defer is allowed only for the existing supported edge events: merged PR and trusted `agent-rework`. Unsupported PR events keep existing behavior.
- If durable defer itself cannot be established/woken, preserve current retryability by surfacing failure so the router releases the delivery dedupe claim.
- Later intake/edge reconciliation must derive current state from fresh GitHub/Kanban evidence.

## 6. Validation contract

Required repository-local validation:

```bash
python3 tests/test_intake_wake_liveness.py
pytest -q tests/test_intake_wake_liveness.py
python3 tests/test_github_router_completion_comment_wake.py
python3 tests/test_periodic_full_intake_fallback.py
python3 tests/test_github_router.py
python3 tests/test_intake_lease_controller.py
python3 automation/n8n/scripts/validate.py
python3 -m py_compile automation/n8n/github-router/router_entrypoint.py automation/n8n/lease-controller/controller.py tests/test_intake_wake_liveness.py
git diff --check
```

`NOT RUN != PASS`. GitHub Actions remain disabled by repository policy.

## 7. Operator acceptance

The final gate is the Ubuntu host canonical deployment/read-back from `docs/OPERATIONS.md`:

- source/live SHA equality for changed runtime sources;
- router, lease-controller, actuator health;
- existing queued CtrlHangul scope is claimed without state-file editing;
- fresh read-back of PR #115 shows `agent-rework` consumed to `agent-working` when the canonical edge claims the new round;
- CtrlHangul root `t_b03e4f35` shows the corresponding new runnable/running rework round.

The H4V3 host control surface became unavailable during this operator session, so host deployment/read-back remains `NOT RUN` until that surface is restored.