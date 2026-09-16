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

Hermes project IDs are profile-local because each profile owns its own `projects.db`. Cross-profile orchestration must not assume that the same repository has the same `p_*` project ID in different profiles. Resolve the project in the creator profile's active `HERMES_HOME`; when project identity must remain portable across profile boundaries, use the canonical project slug together with the verified primary repository anchor rather than treating a raw project ID as a global repository identity. A child specialist may therefore persist a different `project_id` from its root task while still being correctly bound to the same repository slug and anchor.

Do not fall back to `hermes kanban create`, `git worktree add`, or ad-hoc branch creation merely because a structured create is rejected. A rejection means the structured binding/provenance is incomplete or unsafe; fix the structured payload or surface the controller/operator blocker instead of bypassing the creation boundary.

### Dependency waiting state

When Main creates a downstream specialist that must wait for one or more parent tasks, encode that wait with `parents` and leave the child on the normal dependency path. Do not use `initial_status=blocked` merely because a parent is still open. Hermes resolves that ordinary dependency wait as `todo` and the controller promotes it when the parents become terminal.

`blocked` is not a synonym for "not runnable yet". Reserve an initially blocked specialist for an explicit human/operator hold that is independent of ordinary parent completion. In particular, the normal `Developer -> Reviewer` graph is `Developer running` plus `Reviewer todo (parent=Developer)`, not a pre-blocked Reviewer.

### GitHub-backed root join

For every GitHub-backed bounded specialist graph, the graph must close back onto the existing intake root before Main releases its worker slot. The current graph's terminal specialist — normally `kanban-reviewer`, or the approved final design-review specialist when that phase is the terminal gate — must be encoded as a **direct parent of the intake root** through the canonical Kanban dependency mutation surface.

A task-body/comment line such as `Root Kanban task: t_...` is provenance only and does not satisfy this join. After creating the join, Main must fresh-read the root dependency graph and verify the exact terminal specialist appears as an unresolved direct parent while that specialist is non-terminal. If the join cannot be created or verified, fail closed and surface the lifecycle/provenance problem; do not call root `kanban_complete` and do not let the root fall through to GitHub edge projection.

Once the verified terminal-specialist -> root join exists, Main stops consuming a worker slot through the ordinary dependency wait path. The root becomes eligible for core `kanban_complete` only after that current terminal parent is `done` or `archived`. A later rework round must attach its new terminal specialist to the same root before Main yields again; already-terminal historical parents may remain as durable history.

Investigator may be omitted only for a genuinely mechanical task with no meaningful Issue/PR/history synthesis and no implementation decision that investigation could change. Never omit it for regressions, existing-PR rework, runtime-vs-test mismatch, a failed prior root-cause hypothesis, or repeated implementation rounds.

If Reviewer returns REWORK because of a clear local implementation mistake while the root-cause model remains valid, Main may send bounded rework directly to Developer using the existing Investigator handoff. If runtime evidence contradicts tests, the failure boundary remains unclear, the prior hypothesis failed, or the same problem survives rework, create a fresh Investigator phase before another Developer round.

All H4V3 specialist tasks including `kanban-investigator` use `completion_contract=local-only` (or omit it so Hermes defaults to local-only). GitHub acceptance/merge remains root-card edge authority.

### Bounded Investigator search

The default remains one Investigator. Do not fan out merely because a task is
GitHub-backed or because a second opinion is inexpensive.

For a normal task, the graph is:

```text
Main -> Investigator A -> Main selector -> Developer -> Reviewer -> Main
```

The selector is a bounded `kanban-main` phase. Developer must depend on the
selector, never directly on Investigator A. The selector may accept the one
closure-sufficient candidate without creating another Investigator.

Use exactly one of these deterministic trigger codes when search is justified:

```text
LOW_CONFIDENCE_OR_BLOCKING_UNKNOWN
IMPLEMENTATION_OR_REWORK_ROUND_GE_2
RUNTIME_TEST_CONTRADICTION
TRUSTED_REVIEWER_MODEL_REFRESH
COMPETING_BOUNDARIES_UNRESOLVED
NEW_EQUIVALENCE_CLASS_BYPASS_AFTER_PASS
ACCEPTANCE_PASS_RUNTIME_ORACLE_FALSE_NEGATIVE
```

When a trigger is known before dispatch, create exactly two sibling,
independent Investigator tasks (`candidate_id=A` and `candidate_id=B`) and one
selector with both Investigator task IDs as parents:

```text
Main -> Investigator A ─┐
                        ├-> Main selector -> Developer -> Reviewer
Main -> Investigator B ─┘
```

When the first handoff reveals a trigger, the current selector may either
close over A (`selection_status=selected`/`no_selection`) or perform one
bounded expansion by creating B and a final selector over A+B. It must never
recursively expand, create candidate C, or make Developer ready before the
final selector is done. The hard fan-out bound is two total candidates and one
expansion per `search_id`; a duplicate/idempotent replay does not consume a
new candidate slot.

The canonical lifecycle pre-tool guard consumes the same nested marker on
`kanban_create` and on executable terminal/wrapper commands. It records an
atomic admission ledger in the existing `task_events` table before allowing the
mutation; no second dispatcher or database is involved. Candidate IDs other
than A/B, non-matching selector parents, fan-out values other than 2/1, missing
candidate runtime caps, missing root/idempotency binding, and exhausted
numeric admission are rejected before task mutation. A pending selector may
name A once (`selection_status=awaiting_expansion`); it may perform exactly
one B expansion, after which the final selector must name A and B.

Every search task and selector records a nested
`investigation_search` object with schema `h4v3-investigation-search-v1` in
durable task/run completion metadata. It includes `search_id`, `root_task_id`, a stable `idempotency_key`,
`candidate_id` (`A`/`B` for candidates), `phase` (`candidate`/`selector`),
trigger codes, candidate task IDs, selected candidate task ID or null,
selection status and reason, closure comparison, independence basis, and an
explicit `budget`.
Use task IDs and immutable source-provenance references for identity; never
copy the other candidate's transcript or handoff into a candidate body.

The budget is a numeric admission contract, not prose. Every marker must carry
`max_candidates=2`, `max_expansions=1`, a positive `max_runtime_seconds` no
larger than 900, `max_total_tokens` no larger than 32000, and
`max_retries=2`. The guard reserves half of the cumulative token budget (rounded
up) and one retry reservation per candidate in the existing task-event ledger;
concurrent/replayed requests use the same transaction and idempotency key.
`max_runtime_seconds` remains the real dispatcher wall-time cap. Structured
requests and terminal commands must carry the same marker, root task ID, and
stable idempotency key; terminal JSON bodies are normalized into the same
admission function. Missing, zero, non-numeric, inconsistent, or exhausted
values fail closed. Prompt wording, `goal_max_turns`, and post-run telemetry
are not token/retry enforcement and must never be used as substitutes.

### Slow-local worker runtime and retry policy

All newly created H4V3 execution cards must carry an explicit dispatcher wall-time
cap and `max_retries=5`; do not leave either field unset. Use 1800 seconds (30
minutes) for Investigator candidates and bounded Main selectors, 3600 seconds (60
minutes) for Developer/Reviewer/Designer work, and 5400 seconds (90 minutes) only
for an explicitly large implementation whose task body records `runtime_class=large`.
A max-runtime timeout and a clean-exit lifecycle protocol violation both consume the
same five-attempt safety budget; rate-limit exits do not. Do not shorten these values
merely because a cloud model would finish faster: the production local model may spend
several minutes in one reasoning/tool round.

The selector rejects contradictory, duplicate/non-independent, LOW, or
closure-incomplete candidates. Among remaining candidates it records an
evidence-based comparison of failure-boundary fit, preserved contracts,
generalized-invariant/equivalence coverage, falsification strength, completion
oracle, residual unknowns, confidence, and validation cost. It must select one
candidate only when the evidence clearly closes the problem space; otherwise
it records `selection_status=no_selection` and does not create a Developer
task. There is no majority vote or arbitrary numeric score.

Developer context contains the selected handoff only, plus references to the
search and unselected task IDs for provenance. Unselected candidate content is
not copied into the Developer body, prompt, or completion metadata.
