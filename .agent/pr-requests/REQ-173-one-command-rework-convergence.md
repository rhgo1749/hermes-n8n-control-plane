# REQ-173: false-terminal rework recovery one-command convergence

- Status: Implementation
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT` / `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `hotfix/173-one-command-rework-recovery`
- Source-of-truth base: `a60f0a4` (`origin/main` observed before implementation)
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-173-one-command-rework-convergence.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#173`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/173`
- Kanban task ID: `t_7272c744`
- Investigator handoff: `t_73b7c585` (closure-sufficient, Approach A)
- Root intake task: `t_5aa9e0a1` (provenance only; do not modify)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:173`
- Planning/lead owner: `t_73b7c585` investigator handoff
- Implementation owner: `t_7272c744` kanban-developer
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## 0. Mandatory repository route

`AGENTS.md` → `README.md` → `docs/README.md` → `docs/EDGE_REWORK_LIFECYCLE.md` and `docs/GITHUB_COMPLETION_LIFECYCLE.md` → `edge/kanban-github-sync.py` → deterministic edge/safety-wake tests.

## 1. Objective

From one fresh, trusted `agent-rework` label, converge
`DONE + OPEN PR + stale agent-working` through false-terminal repair to
`REVIEW → READY → edge claim → agent-working` without a second maintainer
label command, while preserving strict current-round delivery and request/head
binding.

## 2. Confirmed background and design decision

RC1 repairs false-terminal `DONE + OPEN PR` to `REVIEW`. The prior behavior also
projected `agent-review-ready`, causing the same pending `agent-rework` to be
compared against the newer parking event that RC1 itself created. The command
was consequently treated as stale until a maintainer removed and re-added it.

Implement the investigator-selected Approach A: RC1 removes stale execution and
output labels but does not synthesize `agent-review-ready`; the exact trusted
GitHub timeline label-event id and audit fields are recorded on the parking
`github_pr_sync` event. The existing strict `REVIEW → READY` intake consumes the
unchanged command on the next ordinary wake, and the edge dispatch lane owns the
claim/label swap.

## 3. In scope

1. Preserve stable GitHub `agent-rework` label-event identity in rework
   decisions and durable `github_pr_rework` evidence.
2. Record recovery causality (`recovery_pending_rework_event_id`, label time,
   actor, reason, PR and head) on the RC1 parking event.
3. Suppress unearned `agent-review-ready` during false-terminal conflict repair
   and remove any stale output label in that recovery transition.
4. Add deterministic incident-ordering coverage proving one command reaches a
   running edge-owned round, plus the safety-wake no-success diagnostic.

## 4. Explicit non-goals

- No Hermes core changes, new `agent-*` labels, second dispatcher, polling
  cron, n8n lifecycle ownership, direct SQLite repair, merge, or auto-merge.
- Do not weaken current-round delivery/head/request binding or allow historical,
  untrusted, or malformed label evidence to reopen a round.
- Do not alter merge convergence, human-only merge authority, or the live
  CtrlHangul PR #115 round.
- Do not deploy before merge or claim host/runtime validation from repository
  tests.

## 5. Validation contract

Required local evidence:

```bash
PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py
python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-rework.py tests/test_completion_dispatch_safety_wake.py
python3 -m pytest -q tests/test_completion_dispatch_safety_wake.py
python3 automation/n8n/scripts/validate.py
bash -n automation/hermes/scripts/deploy-intake-edge.sh
 git diff --check
```

`NOT RUN != PASS`; GitHub-hosted Actions are disabled by repository policy.
The canonical deploy/read-back and post-merge deterministic runtime fixture are
host gates and remain pending at implementation handoff.

## 6. Final automation stop state

`HOST_VALIDATION_REQUIRED`: implementation and local deterministic validation
may complete, but deployment through `automation/hermes/scripts/deploy-intake-edge.sh`,
source/target hash read-back, restart/canary, and post-merge live acceptance are
not performed by this pre-merge worker. Merge remains human authority.
