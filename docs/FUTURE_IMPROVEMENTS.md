# Future improvements — intentionally not implemented in this migration

The completed scope is limited to n8n installation/persistence, the GitHub
agent-ready intake schedule and optional event wakeups, preserved Hermes
execution, rollback, and operations evidence.

Do not start these items as part of this migration:

1. **Dispatcher redesign** — retain existing Hermes dispatcher, claim, workspace, spawn, worker session, and Kanban execution paths.
2. **Kanban state-model changes** — Kanban remains the existing task UI/projection/history surface.
3. **New dispatch or completion REST protocols** — the migration uses existing Hermes cron routes only.
4. **An n8n-owned idempotency database** — existing GitHub intake identity/idempotency remains authoritative.
5. **n8n concurrency/retry engine migration** — existing Hermes cron/ticker guards remain in use; no policy engine is moved.
6. **Direct GitHub-payload business logic** — optional GitHub Trigger workflows merely wake the existing intake/reconciliation script. Replacing its polling/reconciliation logic requires a separately scoped design and proof.
7. **Hermes core fork or extraction** — the only adapter is a user plugin that authorizes exact existing routes; no core files are modified.
8. **Public ingress selection** — domain, reverse proxy, TLS, and public exposure require an explicit host/network decision. Until then, n8n stays loopback-only and GitHub Trigger workflows remain inactive.
9. **Request-scoped profile authorization** — the existing generic token seam authorizes route paths but does not pass a `profile` query into providers. The installer currently fail-closes on non-unique job IDs; a formal profile-bound token interface would require a separately authorized core or adapter design.
10. **H4V3 Broadcast Health Monitor n8n-native redesign** — `27f6725028ff` remains an existing Hermes-owned active cron job. Do not add an n8n workflow, scheduler, or health-policy replacement without a separately scoped design.
