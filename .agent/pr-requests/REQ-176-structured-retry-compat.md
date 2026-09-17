# REQ-176 — structured retry compatibility hotfix

## Source

- GitHub Issue: #176
- Repository: `rhgo1749/hermes-n8n-control-plane`
- Base: `main` at `a60f0a43a724fd45a470847d588b27dc63063a57`
- User authorization: hotfix, merge, and deployment explicitly requested.

## Objective

Unblock H4V3 bounded rework/search creation without changing Hermes core when the current model-facing structured `kanban_create` schema omits `max_retries`.

## Scope

- Preserve n8n as bounded event glue; no lifecycle ownership moves into n8n.
- Preserve existing lifecycle guard ordering and search admission ledger.
- Internally canonicalize eligible structured execution creates to task-local `max_retries=5`.
- Only after existing admission succeeds, materialize through the existing core-backed workspace binding owner and verify durable `max_retries=5`.
- Apply compatibility to Investigator/Developer/Reviewer/Designer cards and canonical bounded `kanban-main` selector cards only; ordinary Main creates remain untouched.
- Reconcile the stale Main profile search runtime text from 900 seconds to the executable/canonical 1800 seconds.

## Retry semantics

- `investigation_search.budget.max_retries=2` is a bounded search-admission reservation budget.
- execution-card `max_retries=5` is an automatic timeout/protocol-failure retry budget for one card.
- neither value limits trusted human rework rounds.
- an exhausted card is preserved; a later fresh trusted `agent-rework` opens a new round with new task/idempotency/search identities and fresh per-card five-attempt budgets.

## Non-goals

- No Hermes core source/schema modification.
- No CLI/DB bypass by agents.
- No reset of exhausted historical cards.
- No change to `agent-*` label semantics or merge authority.

## Validation

Required before merge:

- `python3 -m py_compile automation/hermes/scripts/kanban-block-kind-guard.py`
- `pytest -q tests/test_kanban_retry_compat_hotfix.py`
- existing focused lifecycle/workspace tests when the runtime checkout is available.
- profile contract read-back confirms `max_runtime_seconds<=1800`, search budget `max_retries=2`, and execution task retry budget 5.

## Deployment / stop state

Deploy through repository-owned scripts against the live Hermes runtime home. Re-read the deployed global/profile lifecycle guard and `kanban-main` SOUL after deployment. Stop after fresh read-back proves the tracked wrapper/profile are live; do not synthesize a Kanban/GitHub lifecycle transition as part of deployment.
