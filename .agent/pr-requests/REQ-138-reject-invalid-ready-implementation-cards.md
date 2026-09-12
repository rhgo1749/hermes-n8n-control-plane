# REQ-138: invalid ready implementation/REWORK card preflight

- Source Issue: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/138
- Source Kanban task: `t_23107961` (round-2 rework; prior implementation: `t_2121aa1f`); investigator: `t_9a4f7720`; root intake: `t_7ec21f55`
- Base: `origin/main` `a0432f46d94c2baa0d39a011c9d924ff5fd76e66`
- Delivery branch: `fix/issue-138-ready-binding-preflight`
- Pull Request: PR #149 (https://github.com/rhgo1749/hermes-n8n-control-plane/pull/149); implementation commit: `e8430d8f585d71aed45defd96941a12860e2805e`; final branch update also contains this REQ handoff
- Validation: local static/compile, focused binding/parser/completion regressions (`58 passed`), workspace admission/self-heal, edge rework, deployer dry-run; full `pytest -q tests` attempted (`422 passed, 4 failed, 23 errors`, with failures/errors outside the changed paths); GitHub Actions disabled by repository policy
- Automation stop: no live deployment or config-hook mutation; PR handoff only, with human-only merge authority

## Bounded scope

At the approved stable `kanban-block-kind-guard.py` pre-tool boundary, classify exact specialist assignees from structured fields and require deterministic project/repository binding before implementation/REWORK materialization. Resolve the project primary git anchor, let the existing Hermes `create_task` owner derive `<repo>/.worktrees/<task-id>` plus its dedicated project branch, serialize same-key preflight materialization, and immediately read back the task row and parent links. Fail closed on scratch/null/shared/outside-worktree/unverifiable-anchor/kind-path-branch/idempotency mismatches. Preserve explicit `initial_status=blocked` quarantine, reviewer parent direction, spawn-time admission/self-heal, and valid worktree behavior.

## Non-goals

No Hermes core edits, product-repository changes, second dispatcher/state store, polling cron, archived cleanup, GitHub merge/auto-merge, or live deployment claim. The PR must contain visible plain-text `Closes #138.` and merge remains human-only.
