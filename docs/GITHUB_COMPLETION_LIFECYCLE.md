# GitHub-backed Kanban completion lifecycle

GitHub-backed Issue intake cards use deliberately separate lifecycle authorities:

- **Hermes core** owns worker-run termination. A root worker whose required internal task graph is satisfied terminates its run with `kanban_complete`.
- **Kanban specialists** own bounded work products: Developer implements, Reviewer verifies, and Designer decides/reviews user-facing behavior when needed.
- **The control-plane Controller/edge** owns deterministic queue/resource/lease/stale recovery and GitHub completion projection. Fresh GitHub state determines whether the card is parked in `review` or is authoritatively `done`.

The durable role boundaries are defined in `docs/KANBAN_ROLE_CONTRACTS.md`.

The external event path is `github-router` (HMAC, delivery dedupe, and managed
repository admission) → private n8n Webhook (bounded PR-event filter) → the
loopback actuator → `kanban-github-sync.py --board <slug> --json`. The actuator
reuses repository-registry/task-provenance board authority and the existing
edge state machine; n8n does not write Kanban state directly. The tracked
workflow has no Schedule Trigger or polling fallback.

## Why worker `kanban_request_review` is forbidden here

A GitHub-backed worker already has an external review surface: its linked GitHub pull request. Calling core `kanban_request_review` creates a second internal review lane. With the same/default Kanban profile, that review card can be claimed again by the implementation worker, producing a `review -> running -> review` self-review loop while the PR is simply waiting for a human merge.

The intake wrapper therefore emits this contract for new GitHub-backed cards:

1. use real Kanban dependencies for specialist sequencing; Main does not stay RUNNING to poll a child worker;
2. Developer finishes implementation, required repository-local deterministic validation, and PR create/update, records the current PR/head plus any unavailable external/manual gate, then hands off;
3. Reviewer verifies the current PR/head/diff/evidence and returns PASS or REWORK; it does not wait for a future CI/merge event;
4. no implementation, review, or lead worker stays RUNNING solely to wait for GitHub Actions/checks, human review, merge, or future comments;
5. once the root card's required internal graph is satisfied, finish the root worker run with core `kanban_complete`;
6. treat that core `done` as provisional, not as GitHub merge evidence;
7. never call `kanban_request_review` for the GitHub-backed intake card;
8. let `kanban-github-sync` re-query GitHub and project `DONE + required PR OPEN/closed-unmerged -> REVIEW`;
9. the existing `DONE -> REVIEW` edge transition clears `assignee`, claim/worker metadata, blocker metadata, and `completed_at`, leaving a parked review card;
10. only a trusted rework signal may return that parked card to runnable work;
11. only fresh GitHub evidence may project `REVIEW -> DONE` authoritatively. Normally every effective linked required PR must be merged into the target branch. A historical `closed + unmerged` PR may be excluded only when the source Issue is closed, no linked PR remains open, and a newer linked PR with the exact same non-empty head ref is merged into the target branch. Different-head, ambiguous, or still-open lineages remain `review`.

A one-time snapshot of current CI/check state may be recorded in a handoff when relevant. Future external state is not a reason to keep a scarce worker process alive. `NOT RUN` remains distinct from `PASS`; an unavailable required external/manual gate must be reported honestly rather than waited on by polling.

GitHub lookup failures remain fail-closed. Hermes core is not modified.

## Deployment

`automation/hermes/scripts/deploy-intake-edge.sh` deploys:

- `github-agent-ready-kanban-intake.py` — small live wrapper;
- `github-agent-ready-kanban-intake-core.py` — canonical intake implementation;
- the existing edge wrapper/core/overlays and repository registry.

The canonical intake source remains `automation/hermes/scripts/github-agent-ready-kanban-intake.py`. The live wrapper fail-closed overlays two rendered blocks: the GitHub completion contract and the Kanban lead orchestration contract. If either canonical source block drifts unexpectedly, the wrapper refuses to emit an unverified lifecycle contract.
