# Completion dispatcher safety wake — implementation note

> Canonical lifecycle owner: [`GITHUB_COMPLETION_LIFECYCLE.md`](GITHUB_COMPLETION_LIFECYCLE.md).
> This file records the focused safety-wake implementation shape; it does not define a second completion lifecycle or override the canonical document.

`github-completion-edge-wake` remains the primary completion observer. It runs
inside the worker process after `kanban_complete` commits and asks the canonical
GitHub/Kanban edge to park an open PR in `review`.

The companion `github-completion-dispatch-safety-wake` plugin is a bounded
liveness backstop for the case where that worker-side observer is silently
missed. It uses the already-running Kanban dispatcher tick as a trigger; it does
not create another scheduler or poll GitHub.

On a tick it reads only the current board and selects recent `completed` events
whose task is still `done` and whose importer-owned provenance says
`source: github-issue` or `completion contract: github-pr`. It then replays the
primary completion observer for one such task. The canonical edge reconciles
the whole board.

Safety properties:

- no direct Kanban status write;
- no GitHub polling in the safety plugin;
- no cron or sleep loop;
- no second durable state store;
- at most two replay attempts per completion event per process;
- ordinary tasks, stale historical completions, and already-projected tasks are
  ignored;
- the canonical edge lock/idempotency contract remains authoritative.

The companion plugin requires the primary completion plugin and deployed edge
runtime to be present. Install it through
`automation/hermes/scripts/install-github-completion-dispatch-safety-wake.sh`
and restart the long-lived Hermes gateway/dispatcher so the new hook is
registered. The durable ownership, deployment order, and completion semantics
remain those in `GITHUB_COMPLETION_LIFECYCLE.md` and `OPERATIONS.md`.
