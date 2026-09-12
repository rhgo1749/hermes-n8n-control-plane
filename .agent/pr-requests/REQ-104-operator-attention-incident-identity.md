# REQ-104 — scoped Telegram attention and taxonomy parity

- Status: Fresh round-16 integration handoff
- Product type: `EDGE_RECONCILIATION` / `CONTROL_PLANE_AUTOMATION`
- Source Issue: `rhgo1749/hermes-n8n-control-plane#104`
- Existing delivery: PR #129, branch `issue104-operator-attention-incident-identity`
- Kanban task: `t_9aaec83c`; investigator handoff: `t_5bbd549b`; prior implementation: `t_9318cfed`; intake root: `t_5ffdb93b`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:104`
- Authoritative base: freshly fetched `origin/main` @ `2ab75f2e8b26eeb7066986b0da5ea7f939bcd670`
- Required delivery: update existing PR #129 only; no new PR, GitHub merge/auto-merge, or force-push
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `HERMES_PLUGIN`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED` (real Telegram/deployed runtime remain outside this worker)
- Round-16 integration: prior PR head `fdd91736fee7f5542f63a62ce288f90b4c1a7d0a` merged with the authoritative base above; this request intentionally records the resulting merge as `this commit` rather than a self-referential SHA
- PR read-back target: PR #129, base `main`, branch `issue104-operator-attention-incident-identity`, visible `Closes #104.` and GraphQL `closingIssuesReferences=[104]`

## Objective

Repair only the two round-14 defects confirmed by the Investigator: retain canonical
`board_context` scope for unresolved `dispatch_lock_failed` and
`dispatch_lock_unavailable` Telegram observer state, and classify
`rework_attention_label_projection_failed` plus
`rework_retry_label_projection_failed` identically in edge and intake.

## Required behavior

- Structured notification scope must be canonical board context, never parsed from
  display text or borrowed from task/PR context.
- Same unresolved reason on two boards sends independently; repeated same-scope
  incidents and display-only changes stay suppressed while active.
- A successful empty observation replaces only its observed scope. Unobserved or
  failed scopes stay active; a later disappearance/reappearance alerts once again.
- Extend the existing Telegram state file with safe versioned scope ownership.
  Legacy unscoped active state must fail open and never suppress a new scoped alert.
- Both label-projection reasons must use current canonical rework identity when the
  round exists, and remain visibly unresolved/fail-open without PR-only identity.

## Ownership and non-goals

Preserve edge lifecycle/H4V3 contracts, observer-only send failures, semantic
rework identity, missing-round fail-open behavior, and existing task-event dedupe.
Do not change Hermes core, n8n transport, Kanban DB/schema, lifecycle labels,
notification transport, or add a second store. Do not alter existing tests except
to express the superseding scoped-state contract, and do not weaken assertions.
The three block-kind matcher assertions were aligned with the current canonical
helper contract, whose `_MATCHERS` and documentation include `kanban_create`.

## Evidence route and validation

Canonical route: `AGENTS.md` → `README.md` → `docs/README.md` →
`docs/H4V3_OVERVIEW.md` and `docs/EDGE_REWORK_LIFECYCLE.md` → affected source/tests.
Run focused notification/Overview regressions, exact edge rework/canonical suites,
the #145 dependency-wait and #144 split-API regressions, compile/import, Ruff,
BasedPyright/LSP, documentation/link, sabotage where behavior changes, and
`git diff --check` gates. Prior round RED evidence remains historical: the round-14
direct harness failed before the edge reason registry fix; round 16 changes only
integration parentage and stale provenance, so no new behavior RED is justified.
The fresh round-16 handoff records exact commands/results, preserves the baseline
Ruff findings on current-main-only paths, and keeps deployed representative-board
read-back and real Telegram delivery as external `HUMAN_VALIDATION_REQUIRED` /
`NOT RUN` gates. Push a real merge/update commit to the existing PR branch, read
back the exact full head plus `closingIssuesReferences=[104]`/`Closes #104.`, and
hand off without merge or runtime claims that were not executed.

Fresh round-16 local results: focused notification/Overview/#145/#144 pytest
`83 passed`; edge rework `925 passed`; attention recovery `8 passed`; delivery
provenance `9 passed`; projection label history `4 passed`; retry guard `8 passed`;
block-kind `62 passed`; terminal convergence `95 passed`; compileall, n8n
validation, Ruff selected checks, documentation/link checks, and diff checks all
passed. BasedPyright reported `3` candidate errors versus `5` on the exact fetched
base with no candidate-only errors; the configured Pyright LSP didOpen/
publishDiagnostics probe reported `0` error-severity diagnostics on all 10 files.
No new RED/sabotage is applicable because round 16 changes only integration
parentage and provenance; deployed representative-board read-back and real
Telegram delivery remain `HUMAN_VALIDATION_REQUIRED` / `NOT RUN`.
