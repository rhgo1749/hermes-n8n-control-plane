# REQ-104 — scoped Telegram attention and taxonomy parity

- Status: Implementation handoff
- Product type: `EDGE_RECONCILIATION` / `CONTROL_PLANE_AUTOMATION`
- Source Issue: `rhgo1749/hermes-n8n-control-plane#104`
- Existing delivery: PR #129, branch `issue104-operator-attention-incident-identity`
- Kanban task: `t_9318cfed`; investigator handoff: `t_2b3f2754`; intake root: `t_5ffdb93b`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:104`
- Authoritative base: fetched `origin/main` @ `b3fc50b76cd00c951b33831045ea58d01c4ac620`
- Required delivery: update existing PR #129 only; no new PR, merge, or force-push
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `HERMES_PLUGIN`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED` (real Telegram/deployed runtime remain outside this worker)
- Implementation commit / verified PR head: `164aeb947709ae466b59219a90c57a6e503b4bc1`
- PR read-back: PR #129, base `main`, branch `issue104-operator-attention-incident-identity`, `Closes #104.`

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
Run focused notification/Overview regressions, exact edge rework harness, split-API
and related canonical suites named by the task, compile/import, Ruff, BasedPyright/
LSP, documentation/link, sabotage, and `git diff --check` gates. RED evidence was
captured before implementation: the new direct harness exited non-zero at the
missing edge reason registry. Final local evidence: notification direct 38 tests;
notification/Overview pytest 65; edge recovery 5, provenance 9, label history 4,
retry 8, block-kind 62, and terminal convergence 95; py_compile, Ruff, and
BasedPyright all pass. A broader 20-file edge sweep was 17/20: two resource
admission files require the profile runtime/config environment, and the unchanged
self-heal-label fixture still fails its pre-existing expected reason. The full
pytest sweep reached 383 passes but has 23 unrelated legacy `tmp` fixture errors
and 4 completion-wake board-pin/module-environment failures. Push a real commit to
the existing PR branch, read back the exact full head plus closing reference
`closingIssuesReferences=[104]`/`Closes #104.`, and hand off without merge or runtime
claims that were not executed.
