# Hermes n8n Control Plane Documentation

This directory is the durable project memory for the external Hermes → GitHub control plane. It is an index, not a second source of truth: each linked document owns its own contract and should be updated with the implementation change that affects it.

## Recommended reading order

1. Root [`AGENTS.md`](../AGENTS.md) for repository-wide agent rules.
2. Root [`README.md`](../README.md) for the current runtime topology and repository boundary.
3. This index to select the smallest canonical document for the task.
4. The selected document, then the target source and tests it names.

Do not preload every document. Use the narrowest route that covers the change surface, and expand only when repository evidence shows an adjacent contract is affected.

## Task routing

| Change surface | Canonical document |
| --- | --- |
| GitHub webhook intake, delivery dedupe, lease ordering, async event flow | [`GITHUB_EVENT_CONCURRENCY.md`](GITHUB_EVENT_CONCURRENCY.md) |
| Rework dispatch, retry, verification-only handling, terminal/review transitions | [`EDGE_REWORK_LIFECYCLE.md`](EDGE_REWORK_LIFECYCLE.md) |
| `kanban_block` kind validation and blocked-state provenance projection | [`EDGE_REWORK_LIFECYCLE.md`](EDGE_REWORK_LIFECYCLE.md) |
| Worker slot/resource admission and dispatch capacity | [`EDGE_WORKER_RESOURCE_ADMISSION.md`](EDGE_WORKER_RESOURCE_ADMISSION.md) |
| Worker completion vs GitHub review/done projection | [`GITHUB_COMPLETION_LIFECYCLE.md`](GITHUB_COMPLETION_LIFECYCLE.md) |
| Kanban role ownership and role-specific authority | [`KANBAN_ROLE_CONTRACTS.md`](KANBAN_ROLE_CONTRACTS.md) |
| `hermes-agent` discovery, checkout/board authority, bootstrap and registry semantics | [`REPOSITORY_REGISTRY.md`](REPOSITORY_REGISTRY.md) |
| Repository-derived board identity cutover (migration/transition/rollback) | [`BOARD_IDENTITY_MIGRATION.md`](BOARD_IDENTITY_MIGRATION.md) |
| Host deployment, service lifecycle, recovery and operator procedures | [`OPERATIONS.md`](OPERATIONS.md) |
| Cross-project H4V3 ownership and read-only overview semantics | [`H4V3_OVERVIEW.md`](H4V3_OVERVIEW.md) |
| Known deferred work that is not part of the current contract | [`FUTURE_IMPROVEMENTS.md`](FUTURE_IMPROVEMENTS.md) |

## Source-of-truth rule

- `README.md` is the current topology/entry overview, not a duplicate owner of every subsystem rule.
- `docs/*.md` documents own durable architecture, lifecycle and operations contracts.
- Source and tests remain authoritative for executable behavior; if a document and implementation disagree, stop and reconcile the mismatch instead of silently inventing a merged rule.
- GitHub Issues, PR descriptions, Kanban state, logs and one-off incident reports are task evidence, not durable documentation unless a lasting contract change is extracted from them.

## Documentation maintenance

Update the owning document in the same change when implementation changes a durable lifecycle, ownership boundary, validation rule, deployment procedure or repository-discovery contract. Avoid copying the same procedure into multiple files; link to the canonical owner instead.
