# REQ-058: contention-safe completion wake retry

- Status: Rework implementation on existing PR #60
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION` / `HERMES_PLUGIN`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`, `EDGE_REWORK`, `HERMES_PLUGIN`
- Integration target branch: `main`
- Required work branch: `wt/t_d91e085f`
- Source-of-truth base: fetched `origin/main` (`bdf52a9e741c1a05e20ce03973970441d25b7d33` at rework start)
- Remote delivery: update existing PR #60 only
- Merge authority: human/user only; no merge or auto-merge
- Source issue: `rhgo1749/hermes-n8n-control-plane#58`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/58
- Kanban provenance: root `t_a6243210`; current rework task `t_2ec0fbaf`; prior implementation `t_d654516c`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:58`
- Implementation owner: `kanban-developer`
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## Objective

Close the contention gap in the existing completion observer without changing
Hermes core: when the first fixed edge child spends its bounded deadline waiting
for the shared edge lock, re-read the committed task row. If an earlier owner
already projected the task away from `DONE`, suppress the duplicate; if the
GitHub-backed task remains `DONE` (or the re-read is uncertain), launch exactly
one fresh-budget retry so the completion cannot be silently stranded. The later
canonical edge run must observe an open/unmerged PR and perform the existing
`DONE -> REVIEW` projection.

## Confirmed route and ownership

- Canonical routes: `docs/GITHUB_EVENT_CONCURRENCY.md`,
  `docs/GITHUB_COMPLETION_LIFECYCLE.md`, `docs/EDGE_REWORK_LIFECYCLE.md`,
  `docs/KANBAN_ROLE_CONTRACTS.md`, and `docs/OPERATIONS.md`.
- Hermes core remains the worker termination owner.
- The completion plugin remains an observer/trigger only.
- `edge/kanban-github-sync.py` remains the sole GitHub↔Kanban transition owner.
- n8n remains bounded webhook glue; no Schedule Trigger, polling fallback,
  second dispatcher, second state store, or direct Kanban write is introduced.

## In scope

1. Preserve/fix the bounded completion wake retry and post-timeout committed-row
   revalidation after shared-lock contention.
2. Add a process/runtime regression covering owner-before-completion snapshot,
   first waiter timeout, fresh retry, and real open/unmerged `DONE -> REVIEW`.
3. Keep canonical concurrency/completion documentation and PR evidence aligned.
4. Run the required repository-local validation matrix and verify the existing
   PR #60 remote state.

## Explicit non-goals

- No Hermes core/protocol/CLI/Kanban schema changes.
- No polling, sleep loop, Schedule Trigger, guessed success, or new state store.
- No host plugin installation/activation/restart, n8n credential binding or
  activation, deployment, signed canary, live board read-back, merge, or
  auto-merge.
- No new PR and no push to `main`.

## Validation contract

Required local evidence is reported separately for focused contention/retry and
completion/plugin regressions, the ordinary/fail-closed matrix, edge single
flight, intake/actuator/router/concurrency contracts, n8n validation, clean
child-marker edge harness, changed-file syntax/static checks, LSP/Ruff baseline,
and `git diff --check`. GitHub Actions are `NOT RUN` under repository policy;
host/manual gates remain `HOST_VALIDATION_REQUIRED`.

## Completion criteria

- [ ] Current PR #60 branch/head/base and ancestry verified from live sources.
- [ ] First contention timeout cannot silently drop the completion signal.
- [ ] Post-timeout revalidation suppresses a duplicate when the task is no
      longer `DONE`; uncertain reads fail closed into the bounded retry.
- [ ] Fresh retry is finite, fixed-argv, bounded, non-polling, and fail-closed.
- [ ] Regression proves canonical edge observes `DONE` after owner snapshot and
      parks the open/unmerged PR card in `REVIEW` with no uncontrolled duplicate.
- [ ] Required local validation is executed truthfully (`NOT RUN != PASS`).
- [ ] PR #60 body/comment evidence is updated only through supported REST/API
      paths when claims change.
- [ ] Final automation stop state remains `HOST_VALIDATION_REQUIRED`; merge
      remains human-only.
