# Future improvements — intentionally not implemented in this migration

The completed scope keeps the existing Hermes GitHub intake job and execution
path, but retires the n8n-owned five-minute polling schedule. GitHub events now
wake the preserved Hermes job through `github-router` and `lease-controller`;
webhook reconciliation is an explicit maintenance action.

Do not start these items as part of this migration:

1. **Dispatcher redesign** — retain existing Hermes dispatcher, claim, workspace, spawn, worker session, and Kanban execution paths.
2. **Kanban state-model changes** — Kanban remains the existing task UI/projection/history surface.
3. **New dispatch or completion REST protocols** — event wakeups use the existing Hermes cron trigger/pause routes only.
4. **An n8n-owned idempotency database** — existing GitHub intake identity/idempotency remains authoritative.
5. **n8n concurrency/retry engine migration** — existing Hermes cron/ticker and lease guards remain in use; no policy engine is moved.
6. **Direct GitHub-payload business logic** — the router only validates/routs events and hands repository scope to the existing intake/reconciliation script. Replacing that business logic requires a separately scoped design and proof.
7. **Hermes core fork or extraction** — the adapter remains a user plugin that authorizes exact existing routes; no core files are modified.
8. **Automatic periodic polling fallback** — `/fallback` remains an operator recovery endpoint, but no tracked n8n Schedule Trigger calls it automatically. Re-introducing periodic polling requires an explicit reliability decision.
9. **Automatic periodic webhook reconciliation** — repository webhook reconciliation is explicit via `automation/n8n/scripts/reconcile-github-router.sh`. A future low-frequency maintenance scheduler may be added only with a separately reviewed ownership/idempotency contract.
10. **Request-scoped profile authorization** — the existing generic token seam authorizes route paths but does not pass a `profile` query into providers. The installer currently fail-closes on non-unique job IDs; a formal profile-bound token interface would require a separately authorized core or adapter design.
11. **H4V3 Broadcast Health Monitor n8n-native redesign** — `27f6725028ff` remains an existing Hermes-owned active cron job. Do not add an n8n workflow, scheduler, or health-policy replacement without a separately scoped design.
