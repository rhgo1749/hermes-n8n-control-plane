# Worker resource admission for edge rework

## Source

Direct maintainer request in ChatGPT on 2026-08-19. No source GitHub Issue.

Observed incident: a rework task had terminal run 201 (`review_requested`) while
its detached worker PID remained alive; the edge rework lane admitted run 202
for the same task. Both workers shared a serial local inference endpoint, so
the stale process consumed the only usable model slot and the older worker
continued trying terminal board mutations after `current_run_id` moved on.

## Scope

- Keep Hermes core untouched in this control-plane change.
- Add an opt-in, provider/model-agnostic resource-capacity gate around the
  existing edge-owned `_dispatch_pending_rework()` lane.
- Count configured matching live worker PIDs across sibling board DBs, including
  ordinary core workers already using the resource.
- Before admitting a replacement rework run for the same task, reap a terminal
  prior Hermes worker only after verifying the live PID's command line belongs
  to that exact task.
- Never terminate an actually active prior run.
- Preserve the existing board dispatch lock, `max_in_progress`, claim-first PR
  label projection, workspace logic, failure breaker, and GitHub lifecycle.
- Make configuration opt-in and generic so parallel-capable providers/profiles
  keep the exact legacy path when they are not mapped to a constrained group.

## Non-goals

- No Qwen, llama.cpp, SGLang, model name, endpoint URL, GPU, or provider
  hardcoding.
- No global rewrite of Hermes core's ordinary READY-task dispatcher in this
  repository.
- No change to PR merge authority, auto-merge policy, GitHub Actions policy,
  n8n concurrency, Telegram policy, or Kanban completion semantics.
- No automatic deployment or merge.

## Configuration contract

```yaml
kanban:
  worker_resources:
    local-inference:
      capacity: 1
      assignees:
        - "local-*"
      stale_worker_grace_seconds: 5
```

Resource names are arbitrary. `assignees` are shell-style profile globs.
No section / empty section / no matching profile means no additional gate.
A profile matching more than one group fails closed rather than depending on
YAML ordering.

## Acceptance

1. No configured resource -> legacy edge dispatcher called directly.
2. Configured groups do not affect unmatched profiles.
3. Capacity is enforced across sibling Kanban board DBs.
4. A normal core worker with a matching profile occupies a slot and blocks a
   new edge rework spawn.
5. A terminal same-task live worker is verified and reaped before replacement
   admission; PID reuse or unverifiable identity is never signalled.
6. An active same-task prior run is never killed and no replacement is claimed.
7. Resource check and claim/spawn are serialized by a host-local resource lock.
8. Delegation is pinned to the candidate task selected for that resource check.
9. Existing `kanban.max_in_progress` and edge board lock remain in force.
10. Existing parallel-provider behavior remains unchanged unless explicitly
    mapped into a resource group.

## Validation profiles

Required local validation before merge:

```bash
python3 edge/test-kanban-resource-admission.py
/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py
automation/hermes/scripts/deploy-intake-edge.sh \
  --hermes-home /home/hermes/.hermes --dry-run
```

GitHub-hosted Actions remain disabled by repository policy. These commands are
not claimed PASS until actually executed on the Hermes/Ubuntu environment.

## Stop state

Open PR for maintainer review. Do not merge, auto-merge, or deploy.
