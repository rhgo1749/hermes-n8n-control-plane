# REQ-058: n8n Webhook 기반 GitHub PR Edge Sync 및 완료 계약

- Status: Ready for review (completion-side wake rework)
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `EDGE_RECONCILIATION` / `N8N_WORKFLOW`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`, `EDGE_REWORK`, `HERMES_PLUGIN`
- Integration target branch: `main`
- Required work branch: `wt/t_d91e085f`
- Source-of-truth base: latest fetched `origin/main` @ `09b6283acc76ece0fae9ea5cfa5e2330c79372cf`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-058-n8n-webhook-edge-sync.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#58`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/58`
- Kanban task ID: implementation `t_d91e085f`; rework `t_421eeba2` (root `t_a6243210`)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:58`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HOST_VALIDATION_REQUIRED` (live n8n import/activation and signed host canary are not repository-local gates)

## Objective

GitHub PR lifecycle events that affect edge completion/rework should wake one
bounded, private n8n Webhook workflow. In addition, a post-commit
`kanban_task_completed` observer wakes the same deployed edge once for a
GitHub-backed root completion. The workflow filters and normalizes only
`pull_request: closed` with `merged=true` and `pull_request: labeled` with the
trusted `agent-rework` label, then calls the fixed loopback edge-sync actuator.
The actuator resolves the repository's authoritative Kanban board and executes
only `kanban-github-sync.py --board <slug> --json` without shell interpolation.

GitHub-backed worker completion remains a provisional core `kanban_complete`
handoff; fresh GitHub evidence and the existing edge state machine own
`review`/`done` projection. Existing Issue intake events, Hermes job ownership,
and unrelated jobs remain unchanged.

## Confirmed contracts and ownership

- `github-router` remains the external HMAC, delivery-dedupe, managed-repository,
  and fail-closed admission owner.
- Router forwards only bounded normalized event data over a loopback,
  token-authenticated n8n hop. n8n is glue/filtering, not a dispatcher,
  completion owner, worker launcher, or second state store.
- `repository_registry.py`/durable task provenance remains the repository →
  board authority. The edge script remains the GitHub/Kanban state-machine owner.
- The existing Hermes job `default:bf431b2a6ba6` and Hermes core are not edited,
  deleted, recreated, renamed, or replaced.

## In scope

1. Replace the tracked n8n Schedule/cron fallback template and generator output
   with one inactive Webhook → normalize → allowlist → fixed actuator workflow.
2. Route signed `pull_request` events through that private workflow while keeping
   existing non-PR intake routing and replay semantics intact.
3. Add a strict actuator edge-sync request contract, authoritative board lookup,
   fixed argv, timeout/busy guards, and output read-back validation.
4. Add regression coverage for merge → `done`, trusted rework → `ready`,
   unsupported/malformed/replay/failure paths, and completion-contract wording.
5. Install the smallest external Hermes plugin/helper that observes committed
   `kanban_task_completed` events, scopes to GitHub-backed rows, validates the
   board/runtime paths, and invokes the existing edge owner with fixed argv.
   Provide candidate/atomic/backup/rollback host installation without editing
   Hermes core.
6. Update the event, operations, registry, and completion documentation plus this
   request file.

## Explicit non-goals

- Hermes core, Kanban schema, dispatcher, worker/worktree/spawn, or completion
  redesign.
- A second idempotency database, task store, or completion owner.
- Direct public n8n exposure, direct GitHub webhook registration to n8n, HMAC
  bypass, arbitrary Execute Command, shell/argv interpolation, polling, sleep,
  retry loops, auto-merge, or base-branch push.
- Hermes core edits, a second completion observer/state owner, or direct Kanban
  writes from the completion plugin.
- Deleting or mutating the preserved Hermes job or unrelated jobs.
- Live host n8n import/credential binding/activation, real GitHub webhook canary,
  or human review/merge; these are handoff gates.

## Required validation and evidence

- `python3 automation/n8n/scripts/validate.py`
- focused router, actuator, workflow-contract, completion/rework regressions
- completion-side runtime flow: core completion → hook → edge reconciliation
  with an open linked PR immediately ending in `review`; ordinary-task,
  invalid-board, and wake-failure fail-closed controls
- `PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 tests/test_completion_edge_wake_plugin.py`
- `python3 -m py_compile` for changed Python files
- `bash -n` for changed shell files
- `git diff --check`
- Final handoff records exact base/head/PR, command exit status, changed files,
  assumptions, unrun host/manual gates, and residual risks. GitHub Actions are
  disabled by repository policy and are not a substitute for local gates.
