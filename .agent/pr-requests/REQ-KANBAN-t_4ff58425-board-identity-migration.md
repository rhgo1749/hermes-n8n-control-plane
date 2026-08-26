# REQ: intake board identity — repository-derived slug/display identity + reviewed migration

- Status: Implemented (PR pending)
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT` (fixture-driven, no live mutation)
- Integration target branch: `main`
- Required work branch: `fix/repository-derived-intake-board-migration`
- Source-of-truth base: latest fetched `origin/main`
- Remote delivery: Required (operator-authorized branch push + single Korean PR)
- Request language: Korean
- Kanban task ID: `t_4ff58425`
- Intake provenance: user-directed decision recorded on the Kanban task (no GitHub
  Issue number for this control-plane task)
- Automation stop state: `NONE` (implementation complete; live migration is a
  separate operator stage and explicitly NOT executed in this task)

## Scope

1. Repository metadata as the sole identity authority:
   - registry (`automation/n8n/scripts/repository_registry.py`) derives
     `display_name` = repository name alongside `canonical_slug`
     (`repository.name.casefold()`);
   - intake (`automation/hermes/scripts/github-agent-ready-kanban-intake.py`)
     carries `display_name` on `RepositoryConfig` (validated as
     repository-derived at load) and exposes it in the tick output;
   - the static `_BOARD_SHORT_NAMES` map is removed; the `GitHub Intake`
     suffix convention and all CtrlHangul-specific aliases/allowlists are
     retired (n8n webhook/event triggering stays generic — repository
     identity policy lives in registry/intake edge code only);
   - new repositories keep the same generic topic-discovery/verified-checkout
     bootstrap path (no per-repository special cases).

2. Reviewed, reversible identity cutover tool
   (`automation/n8n/scripts/board_identity_migration.py`, deployed by
   `deploy-intake-edge.sh`): preflight -> migrate -> transition -> postcheck
   -> rollback with dry-run, content-stamped backups, evidence-gated
   transition, idempotency anchor carry-over (the board-scoped dedup boundary
   for #72-class issues), durable per-anchor checkpoints/reconciliation,
   shared exclusive migration/intake leases with under-lock rescans, and
   fail-closed gates (non-terminal legacy tasks, mixed/ambiguous provenance,
   canonical conflict, unresolvable checkout, incomplete evidence, and
   backup-content drift). Legacy boards are archived (recoverable), never
   hard-deleted. Bootstrap provisioning validates repository-derived slugs,
   verified checkout origin, and existing-board ownership before any create.
   No GitHub mutation, no Hermes core change, no ad-hoc production SQLite
   writes.

3. Runbook + deployed-runtime verification plan:
   `docs/BOARD_IDENTITY_MIGRATION.md`.

## Non-goals

- No live migration/archive/deploy in this task (operator stage after PR
  acceptance).
- No GitHub Issue label/PR/merge state mutation.
- No Hermes core changes.
- No new n8n workflows; the webhook stays generic.

## Validation

- `tests/test_board_identity_migration.py` (25 fixture/integration tests,
  standalone runner, no live mutation).
- `tests/test_repo_scoped_intake.py` (29 fixture tests) plus registry/intake/
  actuator/router suites re-run; `validate.py` n8n contract check.
- Evidence: exact-head test evidence recorded in the PR body and Kanban
  handoff.
