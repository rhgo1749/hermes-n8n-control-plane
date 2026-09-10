# Kanban role ownership contracts

This document is the durable control-plane contract for H4V3 Kanban roles. Runtime profile prompts may contain additional repository-specific guidance, but they must preserve these ownership boundaries.

The goal is simple: **agents decide and execute bounded work; the deterministic controller owns lifecycle state.** No agent worker should stay alive merely to watch time pass or wait for a future external event.

## 1. Hermes Kanban Main Agent

Main is the lead orchestrator.

Owns:

- understanding the durable root task and source Issue;
- identifying scope, non-goals, acceptance criteria, and required gates;
- choosing the smallest specialist graph;
- encoding real dependencies;
- reading completed specialist handoffs;
- creating bounded rework when needed;
- judging whether the root task's internal work is complete.

Does not own:

- default implementation;
- continuous worker-status observation;
- CI/check polling;
- lease/resource/stale-worker reconciliation;
- product design;
- independent technical review.

Waiting rule:

- Kanban dependencies are the waiting mechanism.
- Main must not consume an active worker slot by sleeping or polling while a dependency is running.
- Resume only when durable dependency state provides new evidence.

Normal implementation graph is `kanban-developer -> kanban-reviewer`. Add `kanban-designer` only when a material product/UX decision or design review is actually required.

### Specialist completion-contract boundary

Hermes core supports PR-aware `completion_contract` values for standalone Kanban tasks whose **own terminal condition** is exact-head GitHub acceptance. That is a different lifecycle from an H4V3 specialist phase.

For tasks assigned to the dedicated H4V3 specialist profiles `kanban-developer`, `kanban-reviewer`, or `kanban-designer`:

- create the task with `completion_contract=local-only`, or omit the field because Hermes normalizes omission to `local-only`;
- never attach `OWNER/REPO` or an exact GitHub PR URL as that specialist task's completion contract, including bounded rework on an existing PR;
- never reassign an already PR-aware task to one of those specialist profiles; first keep it on a non-specialist path or use an explicit operator recovery to restore its specialist-compatible completion contract;
- preserve repository, PR URL, branch, and exact head SHA in the task body and structured handoff metadata as evidence, not as terminal policy;
- a specialist's `done` means that specialist phase completed its bounded internal responsibility; it does **not** mean the PR was accepted, reviewed, or merged;
- future GitHub checks, human review, and merge remain external state and must not prevent the specialist from terminating once its required executable work and evidence are complete.

The restriction is role/task-specific, not profile-global GitHub disablement. `kanban-main` is not automatically a PR-acceptance owner merely because it is Main, and ordinary standalone Kanban tasks outside the H4V3 specialist graph may still use Hermes core PR-aware completion contracts when their actual terminal condition is remote PR acceptance.

The deployed control plane enforces this boundary with a fail-closed `pre_tool_call` guard on `kanban_create` plus the literal terminal `create`/`assign`/`reassign` paths. Rejected calls perform no task mutation. A create must be retried with `local-only`; assignment of an already PR-aware task requires explicit operator recovery rather than moving the contract to a different H4V3 graph node.

## 2. Hermes Kanban Controller

Controller is deterministic lifecycle/reconciliation logic, not the default reasoning agent.

Owns:

- event intake and idempotency;
- READY queue projection;
- worker/resource admission;
- lease and ownership bookkeeping;
- stale/orphan recovery;
- deterministic dependency readiness;
- external GitHub state projection;
- retry/reconciliation rules explicitly encoded by the control plane.

Does not own:

- product decisions;
- application implementation;
- code review quality judgments;
- speculative interpretation of ambiguous requirements.

Controller must prefer deterministic state transitions over long polling workers. External state changes should wake/reconcile work rather than require an implementation agent to remain alive.

## 3. Hermes Kanban Developer

Developer owns implementation delivery.

Owns:

- repository investigation needed for the assigned change;
- coding/debugging/refactoring/configuration;
- required repository-local deterministic validation;
- final diff inspection;
- creating or updating the required GitHub PR;
- reporting exact branch/PR/head, tests run, unavailable gates, and remaining risks.

Does not own:

- product/UX decisions when materially ambiguous;
- final independent acceptance;
- Kanban lifecycle reconciliation;
- waiting for future CI, human review, merge, or comments.

Stop rule:

Once the assigned implementation is delivered, required local validation has actually run, and the PR is created/updated, Developer records the **current** external state once and hands off. Pending future CI/review/merge is a handoff fact, not a reason to stay RUNNING.

`NOT RUN` is never `PASS`. If an external/manual gate cannot run now, record it honestly without turning the worker into a monitor.

## 4. Hermes Kanban Reviewer

Reviewer is independent technical verification.

Owns:

- identifying the exact PR/head under review;
- inspecting the actual diff and relevant source;
- checking repository/task contracts;
- validating upstream evidence;
- running the smallest useful deterministic checks when needed;
- returning one verdict: `PASS` or `REWORK`.

Does not own:

- implementing fixes by default;
- creating downstream rework tasks or manipulating task dependencies;
- product aesthetics when a design lane exists;
- waiting for future CI/review/merge events;
- lifecycle/resource reconciliation.

Reviewer evaluates the review surface that exists now. When returning a `REWORK` verdict, Reviewer provides a structured handoff (exact evidence, findings, inspected head, and required validation) and finishes its run. Reviewer must not create child rework tasks. A future external/manual gate may be reported separately, but the reviewer must not remain alive solely to poll for it.

## 5. Hermes Kanban Designer

Designer owns user-facing product/UX decisions and design review.

DESIGN owns:

- user problem and goal;
- flow and interaction behavior;
- information hierarchy and ergonomics;
- user-facing states/edge cases;
- implementable acceptance criteria;
- genuinely unresolved product decisions.

DESIGN REVIEW owns:

- inspection of the actual implementation against the approved design contract;
- one verdict: `PASS` or `REWORK`;
- bounded must-fix findings separated from optional polish.

Designer does not become the default software implementer, technical reviewer, or lifecycle controller.

## Rework graph invariant

When a reviewer reports `REWORK`:

1. **Structured handoff only**: The reviewer returns a structured report containing verdict=`REWORK`, exact path:line findings, inspected PR head SHA, and required validation commands. The reviewer terminates its run without creating child tasks.
2. **Main Agent owns graph recreation**: Only the Main Agent creates the bounded developer rework task and attaches downstream review.
3. **Valid rework graph topology**:
   ```text
   prior implementation / source task (done)
     └── bounded developer rework (todo -> ready)
           └── fresh reviewer (todo)
   ```
4. **No non-terminal parent dependencies**: Never set a blocked, review-waiting, or non-terminal reviewer task as the blocking parent (`parents=[t_reviewer]`) of the developer rework task. Doing so causes an immediate `parents_not_done` deadlock where the developer task cannot start because the reviewer task is not terminal, yet the reviewer cannot finish without developer changes.
5. **Attach review context by reference**: "Attach review dependency" means referencing the reviewer task ID and findings in the developer task body, comments, and metadata—not creating a blocking dependency link from a non-terminal task.
6. **No forced promotion loops**: Never use repeated `promote --force` to bypass `parents_not_done`. Repair the task dependency topology using canonical Kanban/control-plane dependency operations (e.g. `unlink`/`link`/`reassign`), and reserve direct database interventions exclusively for explicit manual operator recovery.

### Internal `REWORK` verdict is not the GitHub `agent-rework` command

The specialist verdict `REWORK` above is an **internal Kanban review result**. It
authorizes Main to recreate the bounded specialist graph described here; it does
not authorize any worker, reviewer, or controller to create, restore, or infer
the PR label `agent-rework`.

The PR-side `agent-rework` label is a separate trusted maintainer one-shot
control-plane command owned by the GitHub ↔ Kanban edge lifecycle in
`EDGE_REWORK_LIFECYCLE.md`. Internal reviewer output must never be translated
into that label merely because both use the word “rework”.

## GitHub-backed lifecycle invariant

For an Issue-backed root card with a linked PR:

1. specialist implementation/review work completes through Kanban dependencies;
2. no specialist remains RUNNING solely to wait for GitHub Actions, human review, merge, or future comments;
3. the root worker terminates with core `kanban_complete` when its required internal graph is satisfied;
4. that core `done` is provisional and is not GitHub merge evidence. Main Agent must not declare work "merged" or "delivered" based on internal graph completion;
5. edge reconciliation projects an OPEN or closed-unmerged required PR to parked `review` and clears worker ownership;
6. only trusted PR-side rework admission may make the GitHub-backed card runnable again; an internal specialist `REWORK` verdict does not synthesize that GitHub signal;
7. fresh GitHub evidence that every required PR merged into the target branch permits authoritative `done`.

This separation keeps scarce worker slots tied to active work rather than external waiting.