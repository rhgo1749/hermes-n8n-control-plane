# REQ-117 — intake checkout self-heal

Source Issue: rhgo1749/hermes-n8n-control-plane#117
Kanban provenance: root `t_70d2ec8a`; implementation task `t_7119d59f`
Idempotency: `github:rhgo1749/hermes-n8n-control-plane:issue:117`
Branch/base: `fix/intake-checkout-self-heal` -> `main`

Scope
- `github-agent-ready-kanban-intake.py`: add one bounded
  `_self_heal_stale_checkout` contract under the existing per-repository lock.
- `github-agent-ready-kanban-intake-entrypoint.py`: use that same contract for
  event-ready reuse and full-fallback origin snapshots.
- Keep strict origin/default-branch/cleanliness/contract validation, disable
  hooks and fsmonitor, fetch only the GitHub default SHA, prove ancestry with
  Git, and advance only with `git merge --ff-only`.
- Isolate full-fallback repository failures and expose
  `{repository, action, reason}` records in `repository_outcomes`.
- Update `docs/REPOSITORY_REGISTRY.md` and preserve onboarding, registry,
  event-scope, edge-sync, and credential-redaction tests.

Non-goals
- No `reset --hard`, force update, history rewrite, broad cleanup, task
  worktree mutation, polling cron, GitHub Actions, Hermes core change, merge,
  or auto-merge.
- Dirty, wrong-origin, detached/wrong-branch, and diverged checkouts remain
  fail-closed and are not automatically cleaned.

Validation profiles
- STATIC_UNIT: py_compile, git diff --check, repository-native onboarding
  scripts, and focused real-Git self-heal/fallback tests.
- Host gates: deployment dry-run and local health probes are attempted here;
  live deploy/restart/signed canary remain operator gates after PR acceptance.

Automation stop state
PR #118 is created and pushed for human review. No merge or auto-merge is
performed. Live deployment/restart/canary is not claimed when the PR is open or
when the worker cannot reach Docker; the final handoff records exact PASS and
NOT RUN evidence plus operator acceptance requirements.
