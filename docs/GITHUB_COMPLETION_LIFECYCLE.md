# GitHub-backed Kanban completion lifecycle

GitHub-backed Issue intake cards use two deliberately separate lifecycle authorities:

- **Hermes core** owns worker-run termination. A worker that has finished implementation terminates its run with `kanban_complete`.
- **The control-plane edge** owns GitHub completion projection. Fresh GitHub state determines whether the card is parked in `review` or is authoritatively `done`.

## Why worker `kanban_request_review` is forbidden here

A GitHub-backed worker already has an external review surface: its linked GitHub pull request. Calling core `kanban_request_review` creates a second internal review lane. With the same/default Kanban profile, that review card can be claimed again by the implementation worker, producing a `review -> running -> review` self-review loop while the PR is simply waiting for a human merge.

The intake wrapper therefore emits this contract for new GitHub-backed cards:

1. finish the implementation run with core `kanban_complete`;
2. treat that core `done` as provisional, not as GitHub merge evidence;
3. never call `kanban_request_review` for the GitHub-backed intake card;
4. let `kanban-github-sync` re-query GitHub and project `DONE + required PR OPEN/closed-unmerged -> REVIEW`;
5. the existing `DONE -> REVIEW` edge transition clears `assignee`, claim/worker metadata, blocker metadata, and `completed_at`, leaving a parked review card;
6. only a trusted rework signal may return that parked card to runnable work;
7. only a fresh GitHub read proving every linked required PR merged into the target branch may project `REVIEW -> DONE` authoritatively.

GitHub lookup failures remain fail-closed. Hermes core is not modified.

## Deployment

`automation/hermes/scripts/deploy-intake-edge.sh` deploys:

- `github-agent-ready-kanban-intake.py` — small live wrapper;
- `github-agent-ready-kanban-intake-core.py` — canonical intake implementation;
- the existing edge wrapper/core/overlays and repository registry.

The canonical intake source remains `automation/hermes/scripts/github-agent-ready-kanban-intake.py`; the wrapper changes only the rendered completion-contract block and fails closed if that block drifts.
