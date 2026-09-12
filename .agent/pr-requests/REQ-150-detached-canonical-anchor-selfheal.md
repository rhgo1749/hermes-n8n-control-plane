# REQ-150 — detached canonical anchor self-heal

Source incident: 2026-09-12 CtrlHangul Issue #112 `agent-ready` intake was received by the GitHub router but no Kanban task was created because `/ws/projects/ctrl-hangul` was left on a clean detached HEAD after PR #111 work. The canonical anchor had HEAD `92d5eb1...`, local `main` `6bda1dd...`, and `origin/main` `db0579e...`. Manual safe recovery (`switch main` + `merge --ff-only origin/main`) immediately restored intake; scoped canonical intake then created task `t_02f63769` and dispatcher claimed it.
Source Issue: rhgo1749/hermes-n8n-control-plane#150.
Kanban provenance: parent intake `t_46bb97af`, investigation `t_914fd721`, implementation `t_144f8950`.
Implementation base: `origin/main` / `c585d168cf5f6d3be8b714520b09d9cbb3e720b8`.
Required implementation branch: `hotfix/150-detached-canonical-anchor-selfheal`.


## Objective

Extend the existing #117 stale/shallow checkout self-heal contract with one narrowly proven detached-anchor recovery case, without weakening dirty/diverged/wrong-origin safety.

## In scope

- When the canonical checkout is clean but `HEAD` is detached, allow recovery only if:
  - repository/origin/default-branch metadata is already trusted by the existing onboarding path;
  - local `refs/heads/<default>` exists;
  - detached `HEAD` is an ancestor of that local default branch (therefore switching cannot discard unique detached commits);
  - Git can switch to that existing default branch without force/reset and without stealing a branch from another worktree.
- Before cleanliness checks or materialization, reject repository-local
  executable configuration and `$GIT_DIR/info/attributes` rather than allowing
  filters, merge drivers, URL rewrites, or remote helpers to run.
- After reattaching, reuse the existing `_self_heal_stale_checkout` contract for shallow handling, target-SHA ancestry proof, bounded fetch, and `merge --ff-only`.
- Preserve fail-closed behavior for dirty checkout, detached HEAD with unique commits, missing local default branch, branch owned by another worktree, wrong origin, wrong repository, divergence, unsafe attributes/materialization, or any ambiguous Git result.
- Add focused regression coverage reproducing the incident shape and negative safety cases.
- Deploy through the existing intake/edge deployment path only after merge; do not mutate Kanban DBs or lifecycle labels as part of the hotfix.

## Non-goals

- `reset --hard`, `checkout -f`, forced branch moves, deleting worktrees, pruning user branches, broad cleanup, or history rewrite.
- Reusing task worktrees as canonical anchors.
- Changing `agent-ready` label semantics, dispatcher ownership, edge lifecycle semantics, or periodic fallback cadence.

## Validation

- RED-before/GREEN-after real-Git detached-anchor regression.
- Dirty / unique-detached-commit / missing-default-branch / branch-in-other-worktree remain mutation-free fail-closed.
- Local Git filter/transport configuration and `$GIT_DIR/info/attributes` remain
  fail-closed without executing their commands.
- Existing stale/shallow/divergence self-heal tests remain green.
- `py_compile`, focused pytest, `git diff --check`, and deploy dry-run.
- Post-merge runtime deployment: source/live hash equality, scoped CtrlHangul intake dry-run/read-back, router/actuator health, no duplicate #112 task.

## Merge/deploy authority

The user explicitly requested hotfix, merge, and deployment for this incident. Merge is allowed only after fresh PR head/mergeability/review/gate re-check. Live deployment must use the repository-owned deployment path and preserve timestamped backups.
## Automation stop state


The implementation phase stops after local validation, PR creation/update, and
truthful evidence handoff. Merge, production deployment, and host/device gates
remain human/operator authority; GitHub Actions are intentionally not required
for this repository.
