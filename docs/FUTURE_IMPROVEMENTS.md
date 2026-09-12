# Future improvements — intentionally not implemented in this migration

The completed scope retires both the n8n-owned five-minute polling schedule and
the legacy Hermes intake cron execution primitive. GitHub events now wake the
fixed direct actuator through `github-router` and `lease-controller`; webhook
reconciliation is an explicit maintenance action.

Issue #127 supersedes the earlier deferred **automatic periodic polling
fallback** item. The router now performs a bounded low-frequency full-intake
safety wake (default hourly, minimum five minutes) using the existing durable
scope queue and canonical direct-actuator intake path. This is not an n8n Schedule
Trigger, does not perform periodic webhook reconciliation, and does not own or
write GitHub/Kanban lifecycle state.

Do not start these items as part of this migration:

1. **Dispatcher redesign** — retain existing Hermes dispatcher, claim, workspace, spawn, worker session, and Kanban execution paths.
2. **Kanban state-model changes** — Kanban remains the existing task UI/projection/history surface.
3. **New generic dispatch or completion REST protocols** — the bounded loopback
   actuators expose only fixed reviewed operations, not a generic task/completion
   or shell API; Issue intake uses the lease-controller/direct-actuator path.
4. **An n8n-owned idempotency database** — existing GitHub intake identity/idempotency remains authoritative.
5. **n8n concurrency/retry engine migration** — the existing durable scope queue, router safety tick, actuator single-flight, and lease guards remain in use; no policy engine is moved.
6. **Unbounded direct GitHub-payload business logic** — the router validates,
   deduplicates, and admits events; n8n performs only an allowlisted filter and
   bounded normalization; the existing edge script remains the sole GitHub ↔
   Kanban business-logic owner. Any broader payload logic requires a separately
   scoped design and proof.
7. **Hermes core fork or extraction** — the fixed actuator remains an external adapter around deployed Hermes-side scripts; no core files are modified.
8. **Automatic periodic webhook reconciliation** — repository webhook reconciliation is explicit via `automation/n8n/scripts/reconcile-github-router.sh`. A future low-frequency maintenance scheduler may be added only with a separately reviewed ownership/idempotency contract.
9. **Request-scoped profile authorization** — the existing generic token seam authorizes only fixed actuator routes and does not accept request-controlled profiles. Any broader profile-bound interface would require a separately authorized design.
10. **H4V3 Broadcast Health Monitor n8n-native redesign** — `27f6725028ff` remains an existing Hermes-owned active cron job. Do not add an n8n workflow, scheduler, or health-policy replacement without a separately scoped design.
