# REQ-117 — canonical checkout self-heal and fallback isolation

Source Issue: #117 (`hotfix(intake): stale·shallow canonical checkout self-heal 및 full fallback 격리`)
Kanban provenance: task `t_7119d59f`; idempotency key not supplied.

## Scope

Implement the repository-owned refresh contract for existing canonical checkouts:

- validate GitHub identity, normalized `origin`, discovered default branch, and
  clean tracked/index/ordinary-untracked state under the per-repository lock;
- prove ancestry with Git before mutation, unshallow shallow repositories, fetch
  the authoritative default-branch SHA, and advance only with `git merge
  --ff-only`;
- rerun the exact SHA, contract-path, and cleanliness gate after refresh;
- fail closed for dirty, detached/wrong-branch, wrong-origin,
  non-fast-forward/diverged, fetch/unshallow, and unsafe materialization cases;
- use the same self-heal helper for strict onboarding, event-scoped existing
  ready checkout reuse, and full-fallback origin snapshots;
- isolate repository-level fallback failures and emit bounded per-repository
  outcomes without changing unrelated repositories.

Non-goals: reset/force/history rewrite, dirty or diverged automatic recovery,
feature/task worktree changes, new polling ownership, GitHub Actions, merge, or
auto-merge.

## Changed surface

- `automation/hermes/scripts/github-agent-ready-kanban-intake.py`
- `automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py`
- `docs/REPOSITORY_REGISTRY.md`
- `tests/test_checkout_self_heal.py`
- `tests/test_full_fallback_isolation.py`

## Validation profiles

Static: `git diff --check`; `python3 -m py_compile` for changed Python files.

Focused runtime: `python3 -m pytest -q tests/test_checkout_self_heal.py tests/test_full_fallback_isolation.py tests/test_repository_onboarding.py tests/test_full_fallback_onboarding_entrypoint.py tests/test_repo_scoped_intake.py`.

Regression bite: the pre-change source (`HEAD`) was exercised against the same
stale shallow fixture and returned `checkout_default_branch_mismatch`; the new
implementation healed the fixture with real Git commands and passed the focused
self-heal suite.

Host/runtime validation remains a separate handoff gate: deployed actuator and
intake copies, service restart/health, and live canary evidence are not claimed
by this request unless executed and recorded explicitly.

## Automation stop state

Implementation, local validation, documentation, and PR handoff are in scope.
Merge/auto-merge and future GitHub review/check lifecycle remain human-owned.
