# REQ-138: invalid ready implementation/REWORK card preflight

- Source Issue: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/138 (REST read-back: OPEN, labels `bug`/`agent-ready`; fresh REST comments: none)
- Source Kanban task: `t_40c876f2` (round-3 rework; controller worktree `/ws/projects/hermes-n8n-control-plane/.worktrees/issue-138-rework-round-3`, branch `wt/t_40c876f2`); prior implementation `t_2121aa1f`; investigator `t_3b02912d` handoff #401; reviewer `t_3661b3b0` rework #399; root intake `t_7ec21f55`; no separate task idempotency key was supplied
- Base: `origin/main` `a0432f46d94c2baa0d39a011c9d924ff5fd76e66`
- Delivery: preserve and update PR #149 (https://github.com/rhgo1749/hermes-n8n-control-plane/pull/149), branch `fix/issue-138-ready-binding-preflight`; atomic implementation commit `f417a19`; fresh GraphQL `closingIssuesReferences` contains Issue #138 and PR remains OPEN/unmerged
- Changed files: `automation/hermes/scripts/kanban-workspace-binding-guard.py`, `tests/test_kanban_workspace_binding_guard.py`, and this REQ provenance document
- Validation: focused guard/parser/completion suite `68 passed`; full suite `432 passed, 4 failed, 23 errors` versus fresh `origin/main` `387 passed, 4 failed, 23 errors` with the same four completion-wake failures and 23 missing-`tmp` fixture setup errors; GitHub Actions disabled by repository policy
- Automation stop: no Hermes core or product-repository edits, no live deployment/config-hook mutation, no GitHub lifecycle mutation, no merge/auto-merge; isolated deployer dry-run only, human-only merge authority

## Bounded scope

At the approved stable `kanban-block-kind-guard.py` pre-tool boundary, keep exact specialist admission and project/repository binding, but make materialization and durable read-back one outer Hermes core `write_txn` on the canonical board connection. A new mismatch rolls back task/link/event rows before any claimer can observe them. A pre-existing malformed idempotent row may be quarantined only by transaction-local CAS while it remains non-running and unclaimed; running/current-run claim metadata is never overwritten. Resolve the lock path via core `kanban_db_path(board=...)` and pass that exact path to core `connect()` so current-board, case-normalized, default, and DB-pinned paths cannot diverge. Preserve valid worktree/branch derivation, exact idempotency keys, parent/reviewer direction, explicit `initial_status=blocked`, admission/self-heal, and stable wrapper/deployer behavior.

## Validation profiles

- Real-core atomic probes (each repeated twice): create/claim barrier, mismatch rollback, canonical resolver path matrix, and concurrent same-key convergence; all pass.
- Static/runtime: Ruff, basedpyright, profile LSP Pyright, `py_compile`, `bash -n`, `git diff --check`, and n8n validator all pass.
- Edge: workspace admission `27 passed`, workspace self-heal `38 passed`, block-kind fail-closed `62 passed`, rework provenance `9 passed`, label-history `4 passed`, attention recovery `8 passed`, terminal convergence `95 passed`, GitHub-sync rework `925 passed`.
- Isolated `deploy-intake-edge.sh --dry-run`: candidate validation passes, candidate is removed, config SHA is unchanged, and no live Hermes path is touched.

## Non-goals

No Hermes core edits, product-repository changes, second dispatcher/state store, polling cron, archived cleanup, GitHub merge/auto-merge, or live deployment claim. The PR retains visible plain-text `Closes #138.` and merge remains human-only.
