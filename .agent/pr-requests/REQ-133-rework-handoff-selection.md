# REQ-133: current-round specialist rework handoff selection

- Status: Implementation complete; PR #134 open
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `hotfix/issue-133-rework-handoff-selection`
- Source-of-truth base: latest fetched `origin/main` `4152ac1572c7c479674410084cfa4ff6cd99014b`
- Remote delivery: Required
- Merge authority: Human/user only
- Source Issue: `rhgo1749/hermes-n8n-control-plane#133`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/133
- Kanban task ID: `t_c2049f66`
- Kanban intake/source card: `t_ad452016`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:133`
- Implementation owner: `kanban-developer`
- Automation stop state: `NONE`

## Objective

Correct edge specialist-graph delivery provenance so a current rework developer → reviewer chain is selected deterministically even when an older reviewer remains a direct parent of the lead. Accept only current-round, terminal, exact-head, validated developer/reviewer evidence; preserve fail-closed behavior for incomplete or ambiguous evidence.

## Confirmed route and ownership

- Canonical lifecycle document: `docs/EDGE_REWORK_LIFECYCLE.md`.
- Source: `edge/kanban-github-sync.py`; existing live overlays and compatibility behavior remain bounded to the edge.
- GitHub remains the review/merge authority, Kanban remains execution state, edge remains reconciliation owner, and n8n remains transport/glue only.
- No Hermes core, runtime DB, live task, PR #111, PR #129, or Issue #104 mutation is in scope.

## In scope

1. Select the current specialist graph from durable task creation/link/run/event evidence rather than treating every historical direct parent as current.
2. Require coherent current rework round, terminal developer validation, terminal reviewer PASS, and matching full PR head attestations.
3. Add deterministic positive and negative regressions for historical-parent, stale-only, ambiguous/missing reviewer, round/head mismatch, and missing developer validation cases.
4. Record local validation and PR evidence; do not merge or auto-merge.

## Explicit non-goals

- Hermes core changes or a second dispatcher/completion owner.
- Title-only/sub-string-only role or verdict shortcuts.
- Changes to lifecycle labels/projections, retry/idempotence semantics, n8n transport boundary, or human-only merge authority.
- Manual durable DB/live runtime changes.

## Validation contract

Run and record exact results for the five required edge regressions, changed-file compile/import/static checks, available LSP/type diagnostics, and `git diff --check`. Include a pre-fix bite showing the historical-parent fixture fails at the authoritative base and a post-fix pass. PR body must contain visible plain-text `Closes #133.` outside code fences.

## Implementation evidence

- Pre-fix bite at base `4152ac1572c7c479674410084cfa4ff6cd99014b`: the added historical-parent fixture selected the edge bootstrap/root instead of the developer attestation and failed its authoritative assertion.
- Post-fix focused result: `test_146_current_round_specialist_chain_ignores_historical_direct_parent` passed; the selected run is the current developer run, not the bootstrap, reviewer, or lead run.
- Post-fix specialist guard result: `9 passed` in `edge/test-kanban-rework-delivery-provenance-guard.py`.
- Post-fix edge lifecycle matrix: `918 passed, 0 failed` in `edge/test-kanban-github-sync-rework.py`.
- Additional regression results: projection/history `4 passed`, attention delivery recovery `5 passed`, terminal convergence `95 passed, 0 failed`.
- Static results: changed Python files compile; focused Ruff undefined-name/import/error checks pass; `git diff --check` passes. Patch-time LSP diagnostics reported no remaining errors for the changed implementation files.
- Known baseline-only result: `edge/test-kanban-rework-attention-selfheal-label.py` fails identically on `origin/main` and this branch in an unrelated entrypoint self-heal assertion; it is outside this change's call path.
- PR handoff requirement: include visible plain-text `Closes #133.` in the PR body; merge/auto-merge remains human authority.
- Final automation stop state: PR #134 is open with implementation evidence delivered; no merge, auto-merge, or CI wait was performed.
