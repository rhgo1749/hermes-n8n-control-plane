# REQ-080: claim-time lifecycle label projection 보강

- Status: Draft
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `issue80/claim-time-projection`
- Source-of-truth base: `origin/main`
- Authoritative base SHA: `630b6efe4126e08933f4b454e45ed39ed3732a24`
- Remote delivery: Required
- Source issue: `rhgo1749/hermes-n8n-control-plane#80`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/80`
- Kanban task ID: `t_5af76d55`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:80`
- Implementation owner: `kanban-developer`
- Automation stop state: `HOST_VALIDATION_REQUIRED`
- Merge authority: Human/user only

## Objective

Make claim-time GitHub PR lifecycle-label projection durable, observable, retry-safe,
and compatible with stale `agent-review-ready` left by a prior `github_pr_sync`
`DONE -> REVIEW` parking transition.

## Confirmed background

- `edge/kanban-github-sync.py` owns GitHub ↔ Kanban lifecycle reconciliation.
- The dispatch path claims `READY`, then atomically swaps `agent-rework` /
  `agent-review-ready` to `agent-working`; failure currently reclaims with only a
  generic reason and can lose the request-side label after a stale read-back.
- Existing stale normalization only trusts a prior
  `github_pr_rework_delivery` timestamp, but the affected parking provenance is
  `github_pr_sync`.
- Hermes core, n8n, H4V3 Overview, Telegram, and the Kanban schema remain outside
  this change.

## In scope

1. Emit a secret-free structured `task_events` diagnostic for claim-stage label
   PATCH non-2xx and read-back mismatch, including task/repository/Issue/PR,
   stage/operation, failure class, status when present, before/desired/observed
   label sets, and retryability.
2. Reclaim failed claims without spawning; preserve `READY` plus
   `agent-rework`, and allow one later edge wake to retry idempotently.
3. Recognize the documented `github_pr_sync` review-parking provenance when
   proving stale `agent-review-ready` is superseded by a newer trusted
   `agent-rework`, while retaining fail-closed behavior for ambiguity/conflict.
4. Add deterministic regression coverage for both failure classes, no-spawn and
   retry success, the parking/rework sequence, and existing conflict/idempotency
   behavior.
5. Update the owning lifecycle documentation only where the durable contract
   changes.

## Explicit non-goals

- Do not modify Hermes core, Kanban schema, n8n architecture, worker protocol,
  H4V3 Overview ownership, Telegram history, or runtime deployment scripts.
- Do not repair Re-Bound PR #113 manually, merge/auto-merge, add polling, or add
  a second dispatcher/state store.
- Do not enable GitHub Actions.

## Ownership and security gates

- Ownership impact: `NONE` — existing edge and Kanban authorities remain.
- New state/store: `NO` — diagnostics use existing `task_events`.
- Security/auth impact: `NONE`; label diagnostics must never contain tokens or
  credential values.
- Host/network/runtime deployment evidence is separate from repository tests and
  is not available in this worker environment.

## Validation contract

Required local evidence:

```bash
env -u HERMES_DELEGATED_CHILD_CONTEXT \
  PYTHONDONTWRITEBYTECODE=1 \
  /ws/hermes-agent/venv/bin/python3 \
  edge/test-kanban-github-sync-rework.py
python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py
python3 tests/test_h4v3_overview.py
python3 tests/test_h4v3_notification_policy.py
git diff --check
```

Executed evidence:

- Focused edge rework regression: `PASS — 769 passed, 0 failed`.
- Focused terminal-convergence regression: `PASS — 95 passed, 0 failed`.
- `py_compile`, `git diff --check`, `automation/n8n/scripts/validate.py`, H4V3
  overview/notification, repository registry, GitHub concurrency/router/actuator,
  intake contract/lease, board migration, completion-wake retry, and edge
  single-flight gates: `PASS`.
- `basedpyright` candidate and `origin/main`: 31 errors each; no new errors.
- `ruff` candidate and `origin/main`: 177 diagnostics each; no new diagnostics.
- Deployment script `--dry-run` with an isolated temporary Hermes home: `PASS`.
- The documented contention test fails identically on clean `origin/main` due to
  its existing `selfheal_pass_failed` result mismatch; it is outside Issue #80.
- The README-referenced `tests/test_n8n_cron_auth_plugin.py` is absent from this
  checkout, so that command is `NOT RUN`.

The canonical deploy/runtime hash verification and bounded canary require host
operator access and remain `NOT RUN — HOST_VALIDATION_REQUIRED` for this worker;
Docker is not installed in this environment, and no live mutation was attempted.

## Understanding handoff

- Before: claim failure returns `READY` only through generic reclaim handling;
  lifecycle failure detail is not durable, and stale review parking is tied to
  delivery-event provenance.
- After: projection errors carry bounded structured evidence, claim failure calls
  the existing label-restore path before returning `READY`, no spawn occurs, and
  the next permitted edge wake can perform exactly one successful label swap.
- Canonical source/consumer: `_project_pr_lifecycle_labels` → claim callback →
  `_dispatch_pending_rework_locked` → existing `task_events` / Kanban row.
- Rejected alternative: a new retry queue or database; existing Kanban state and
  serialized edge wake are the authority.

## Final automation stop state

`HOST_VALIDATION_REQUIRED`: local implementation/edge evidence can be delivered,
but live canonical deploy-script hash verification, installation, and bounded
runtime canary must be performed and judged by an authorized host operator.
