## H4V3 Kanban Investigator routing

When operating as `kanban-main`, use `kanban-investigator` as the normal first specialist for GitHub-backed implementation/rework before creating a Developer task.

Normal graph:

```text
Main
-> Investigator
-> Designer (only when a material product/UX decision is needed before implementation)
-> Developer
-> Designer design-review (only when an approved design contract requires it)
-> Reviewer
-> Main
```

Main reads the root task/source Issue enough to establish identity, authorization, scope, and routing, but detailed Issue/PR/history synthesis belongs to Investigator. Do not make Developer reconstruct the full Issue/PR discussion when an Investigator handoff exists.

### Edge-admitted rework boundary

When this Main run was dispatched by the canonical edge for a specific GitHub PR rework round, the edge has already decided rework admission for that round. Treat the durable current-round `github_pr_rework`/edge-dispatch provenance as authorization to route the specialist graph; do not re-run maintainer-command admission from GitHub comment chronology and do not block solely because `AGENT_REWORK_RETRY` is absent.

A fresh trusted PR-side `agent-rework` command intentionally opens a label-origin round without any retry comment. `AGENT_REWORK_RETRY` is a different one-shot recovery signal used only to open a new round after the canonical `rework_human_attention` hold / operator-recovered REVIEW contract in `docs/EDGE_REWORK_LIFECYCLE.md`. Never require that recovery comment as a second authorization for an already edge-admitted fresh-label round.

If trustworthy edge-dispatch provenance for the current round is missing, malformed, or ambiguous, report that provenance problem to the controller/operator instead of inventing a lifecycle transition or synthesizing a retry requirement.

Developer task context should reference the completed Investigator task/handoff and retain exact source provenance. Raw transcripts are not the handoff.

### Specialist workspace creation

Use structured `kanban_create` as the canonical specialist creation surface. For every dispatchable H4V3 specialist card, pass `workspace_kind="worktree"`, the canonical Hermes `project` id/slug for the repository, and a stable `idempotency_key`. Omit `workspace_path`, `branch`, and `branch_name`: Hermes core derives the task-id worktree under `<repo>/.worktrees/<task-id>` and its deterministic project branch, and the control-plane creation preflight verifies the durable read-back before the card becomes dispatchable.

Do not fall back to `hermes kanban create`, `git worktree add`, or ad-hoc branch creation merely because a structured create is rejected. A rejection means the structured binding/provenance is incomplete or unsafe; fix the structured payload or surface the controller/operator blocker instead of bypassing the creation boundary.

### Dependency waiting state

When Main creates a downstream specialist that must wait for one or more parent tasks, encode that wait with `parents` and leave the child on the normal dependency path. Do not use `initial_status=blocked` merely because a parent is still open. Hermes resolves that ordinary dependency wait as `todo` and the controller promotes it when the parents become terminal.

`blocked` is not a synonym for "not runnable yet". Reserve an initially blocked specialist for an explicit human/operator hold that is independent of ordinary parent completion. In particular, the normal `Developer -> Reviewer` graph is `Developer running` plus `Reviewer todo (parent=Developer)`, not a pre-blocked Reviewer.

Investigator may be omitted only for a genuinely mechanical task with no meaningful Issue/PR/history synthesis and no implementation decision that investigation could change. Never omit it for regressions, existing-PR rework, runtime-vs-test mismatch, a failed prior root-cause hypothesis, or repeated implementation rounds.

If Reviewer returns REWORK because of a clear local implementation mistake while the root-cause model remains valid, Main may send bounded rework directly to Developer using the existing Investigator handoff. If runtime evidence contradicts tests, the failure boundary remains unclear, the prior hypothesis failed, or the same problem survives rework, create a fresh Investigator phase before another Developer round.

All H4V3 specialist tasks including `kanban-investigator` use `completion_contract=local-only` (or omit it so Hermes defaults to local-only). GitHub acceptance/merge remains root-card edge authority.
