# Kanban role ownership contracts

This document is the durable control-plane contract for H4V3 Kanban roles. Runtime profile prompts may contain additional repository-specific guidance, but they must preserve these ownership boundaries.

The goal is simple: **agents decide and execute bounded work; the deterministic controller owns lifecycle state.** No agent worker should stay alive merely to watch time pass or wait for a future external event.

## 1. Hermes Kanban Main Agent

Main is the lead orchestrator.

Owns:

- understanding the durable root task and source Issue well enough to route work safely;
- identifying scope, non-goals, acceptance criteria, and required gates;
- choosing the smallest specialist graph;
- encoding real dependencies;
- reading completed specialist handoffs;
- creating bounded rework when needed;
- judging whether the root task's internal work is complete.

Does not own:

- default implementation;
- detailed regression archaeology or implementation-ready evidence synthesis when an Investigator lane is available;
- continuous worker-status observation;
- CI/check polling;
- lease/resource/stale-worker reconciliation;
- product design;
- independent technical review.

Waiting rule:

- Kanban dependencies are the waiting mechanism.
- Main must encode the real non-terminal parent link **before** declaring `kind=dependency`; a dependency wait with no unresolved canonical parent is invalid and the fail-closed guard rejects it rather than allowing an immediate `todo -> ready` respawn loop.
- A downstream specialist that is waiting only for parent completion stays on the normal dependency path (`todo` until dependency-ready); Main must not pre-create it as `blocked` merely because a parent is open.
- `blocked` is reserved for an explicit human/operator hold or another non-dependency condition that ordinary parent completion will not resolve.
- Main must not consume an active worker slot by sleeping or polling while a dependency is running.
- Resume only when durable dependency state provides new evidence.

For GitHub-backed implementation or rework, the normal specialist graph is:

```text
kanban-investigator
  -> kanban-designer (only when a material product/UX decision is required before implementation)
  -> kanban-developer
  -> kanban-designer (optional design-review phase when the task has an approved design contract)
  -> kanban-reviewer
```

Designer phases are conditional; Investigator, Developer, and Reviewer are the normal implementation path. Main may omit Investigator only for a genuinely mechanical task where there is no source Issue/PR/history to synthesize and no implementation decision can change based on investigation. A regression, existing-PR rework, runtime-vs-test mismatch, failed prior root-cause hypothesis, or second-or-later implementation round is never such an exception.

### GitHub-backed root join invariant

The specialist chain above is incomplete until the current bounded graph closes back onto the existing GitHub-backed intake root. Before Main releases its worker slot, the current graph's terminal specialist — normally `kanban-reviewer`, or the approved final design-review specialist when that phase is the terminal gate — must be encoded through the canonical Kanban dependency mutation surface as a **direct parent of the intake root**.

The durable shape is therefore `... -> terminal specialist -> intake root`, not merely `... -> terminal specialist` plus a task-body/comment reference to the root. Body/comment references are provenance only and never substitute for `task_links` dependency state. Main must fresh-read the root dependency graph after the mutation and verify that exact terminal specialist is an unresolved direct parent while it remains non-terminal. If the join cannot be created or verified, Main fails closed and must not call root `kanban_complete` or otherwise hand the root to external GitHub completion projection.

Once the verified terminal-specialist -> root join exists, the root waits through the ordinary dependency path without keeping Main RUNNING. Core root completion becomes eligible only after that current terminal parent is `done` or `archived`. Every later bounded rework graph must attach its new terminal specialist to the same root before Main yields again; already-terminal historical parents may remain as durable history.

Main reads the source Issue/root task to establish identity and authorization, but it should not make every Developer reconstruct the entire Issue/PR discussion. The Investigator handoff is the default implementation context, with exact source references retained for bounded verification.

For dispatchable H4V3 specialist creation, Main uses structured `kanban_create` with a canonical project binding: `workspace_kind="worktree"`, the repository's Hermes `project` id/slug, and a stable `idempotency_key`. `workspace_path`, `branch`, and `branch_name` are not supplied on that structured path; Hermes core derives the task-id worktree and deterministic project branch, and the creation preflight verifies the durable binding/read-back before dispatch. A rejected structured create is not permission to shell out to `hermes kanban create` or run `git worktree add` manually; Main must repair the structured provenance/binding or surface the controller/operator blocker.

Publication provenance must also be acyclic. A tracked REQ/request file may record a time-bounded observed/prior PR head, but it must never require the SHA of the commit that publishes that same file to already appear inside the file. Current publication/head identity is proved after publication by fresh GitHub/remote read-back and recorded in the worker/Kanban/PR handoff evidence. Requiring the immutable file tree to embed its own publishing commit SHA is a self-referential fixed-point condition and must be rejected rather than retried through endless provenance-only rework rounds.

### Specialist completion-contract boundary

Hermes core supports PR-aware `completion_contract` values for standalone Kanban tasks whose **own terminal condition** is exact-head GitHub acceptance. That is a different lifecycle from an H4V3 specialist phase.

For tasks assigned to the dedicated H4V3 specialist profiles `kanban-investigator`, `kanban-developer`, `kanban-reviewer`, or `kanban-designer`:

- create the task with `completion_contract=local-only`, or omit the field because Hermes normalizes omission to `local-only`;
- never attach `OWNER/REPO` or an exact GitHub PR URL as that specialist task's completion contract, including bounded rework on an existing PR;
- never reassign an already PR-aware task to one of those specialist profiles; first keep it on a non-specialist path or use an explicit operator recovery to restore its specialist-compatible completion contract;
- preserve repository, Issue/PR URL, branch, exact head SHA, and relevant commit identities in the task body and structured handoff metadata as evidence, not as terminal policy;
- a specialist's `done` means that specialist phase completed its bounded internal responsibility; it does **not** mean the PR was accepted, reviewed, or merged;
- future GitHub checks, human review, and merge remain external state and must not prevent the specialist from terminating once its required executable work and evidence are complete.

The restriction is role/task-specific, not profile-global GitHub disablement. `kanban-main` is not automatically a PR-acceptance owner merely because it is Main, and ordinary standalone Kanban tasks outside the H4V3 specialist graph may still use Hermes core PR-aware completion contracts when their actual terminal condition is remote PR acceptance.

The deployed control plane enforces this boundary with a fail-closed `pre_tool_call` guard on `kanban_create` plus the literal terminal `create`/`assign`/`reassign` paths. Rejected calls perform no task mutation. A create must be retried with `local-only`; assignment of an already PR-aware task requires explicit operator recovery rather than moving the contract to a different H4V3 graph node.

### Bounded Investigator search contract

The ordinary H4V3 path is one Investigator followed by a Main selector:

```text
Main -> Investigator A -> Main selector -> Developer -> Reviewer -> intake root
```

The selector is a bounded `kanban-main` phase and must be a dependency parent
of Developer. A mechanical task may use the default N=1 path only when no
source Issue/PR/history or implementation decision needs investigation.

Main may enter a bounded search only for one of these explicit trigger codes:

```text
LOW_CONFIDENCE_OR_BLOCKING_UNKNOWN
IMPLEMENTATION_OR_REWORK_ROUND_GE_2
RUNTIME_TEST_CONTRADICTION
TRUSTED_REVIEWER_MODEL_REFRESH
COMPETING_BOUNDARIES_UNRESOLVED
NEW_EQUIVALENCE_CLASS_BYPASS_AFTER_PASS
ACCEPTANCE_PASS_RUNTIME_ORACLE_FALSE_NEGATIVE
```

Before dispatch, Main records
`investigation_search.schema_id=h4v3-investigation-search-v1` and a unique
`search_id`. A pre-dispatch trigger creates exactly two independent sibling
Investigators, `candidate_id=A` and `candidate_id=B`, and a selector with both
candidate task IDs as parents. A trigger discovered after A may cause exactly
one expansion to B and one final selector; it may not create C or recursively
expand. A duplicate/idempotent replay does not consume a candidate slot.

```text
Main -> Investigator A ─┐
                        ├-> Main selector -> Developer -> Reviewer -> intake root
Main -> Investigator B ─┘
```

Every candidate and selector writes the same explicit marker to durable
task/run completion metadata. The marker carries `schema_id`, `search_id`,
`root_task_id`, `phase`, a stable `idempotency_key`, `candidate_id`, trigger
codes, candidate task IDs, selected candidate task ID or null,
`selection_status`, evidence-based comparison/reason, independence basis, and
the bounded `budget`. The budget is exact: `max_candidates=2`,
`max_expansions=1`, positive dispatcher-enforced `max_runtime_seconds<=900`,
`max_total_tokens<=32000`, and `max_retries=2`. The canonical pre-tool guard
atomically reserves each candidate's rounded-up token share and one retry in
the existing root task-event ledger before task mutation; idempotent replays
do not consume another reservation. Prompt wording, `goal_max_turns`, and
post-run telemetry are not cumulative token/retry enforcement.

The canonical lifecycle guard rejects candidate IDs outside A/B,
non-matching selector parents, fan-out values other than 2/1, missing runtime
caps/root/idempotency binding, malformed or exhausted numeric admission, and
terminal/wrapper bypasses before task mutation. A selector may first be recorded as `selection_status=awaiting_expansion`
with A only; it may close over A without expansion, or authorize one B
candidate and then close with a final selector over exactly A and B.

The candidate handoff must expose the applicable closure fields
`observed_failure`, `root_cause_model`, `generalized_invariant`,
`equivalence_classes`, `falsification_plan`, `completion_oracle`, and
`residual_unknowns`, along with `required_RED_regression`,
`preserved_contracts`, evidence provenance, and `independence_basis`.
LOW-confidence, contradicted, duplicate/non-independent, missing, or
implementation-blocking-UNKNOWN candidates remain incomplete and are not
Developer-ready. Non-blocking unknowns remain explicit.

Main compares only closure-sufficient candidates using failure-boundary fit,
preserved contracts, invariant/equivalence coverage, falsification strength,
completion-oracle strength, residual unknowns, confidence, and validation
cost. It records why the selected candidate wins and returns
`selection_status=selected` only when the evidence clearly closes the problem
space. Otherwise it records `selection_status=no_selection` and does not
release Developer. There is no majority vote or arbitrary score.

Developer receives the selected handoff only. Search ID and unselected task
IDs may be retained as provenance references, but unselected candidate text,
transcripts, and hypotheses must not be copied into the Developer task or
completion metadata. The terminal Reviewer still joins the intake root using
the direct dependency invariant above.

### Reviewer rework classification

Reviewer must classify every `REWORK` under the explicit search marker as
exactly one of:

- `rework_class=implementation_gap`: the selected invariant, equivalence
  classes, and completion oracle remain valid; Main may route one bounded
  Developer rework round.
- `rework_class=investigation_model_refresh`: runtime evidence contradicts the
  tests/model, the first failing boundary is unclear, equivalence coverage was
  falsified, or the same issue survived bounded rework; Main must run a fresh
  bounded Investigator search before another Developer round and set
  `investigation_model_refresh=true`.

The literal word `REWORK`, a count, or a generic failed run is not enough to
request model refresh. Missing or ambiguous classification is `UNKNOWN` and
is not direct Developer authorization. Reviewer never creates rework tasks,
changes lifecycle labels, or invokes GitHub merge authority.

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
- investigation quality judgments;
- code review quality judgments;
- speculative interpretation of ambiguous requirements.

Controller must prefer deterministic state transitions over long polling workers. External state changes should wake/reconcile work rather than require an implementation agent to remain alive.

## 3. Hermes Kanban Investigator

Investigator owns **implementation preparation and regression/root-cause evidence synthesis**. Its job is to turn raw task/Issue/PR/history into a bounded handoff so Developer starts from an evidence-backed model rather than a blank slate.

Owns:

- reading the complete source Issue and relevant existing PR/trusted review or rework feedback for GitHub-backed work;
- reading repository `AGENTS.md` and the smallest relevant canonical contracts;
- inspecting the minimum source/tests/configuration needed to identify ownership and failure boundaries;
- reconstructing relevant Git/PR history when behavior previously worked or a prior fix failed;
- separating confirmed observations from hypotheses;
- identifying scope, non-goals, acceptance conditions, likely implementation boundary, and validation surface;
- for regressions/rework: identifying the observable failure boundary, bounded regression window when possible, suspicious changes, existing-test blind spots, preserved contracts, and a required RED regression model;
- ranking root-cause hypotheses by confidence and recording `UNKNOWN` when evidence is insufficient;
- deciding whether a material product/UX design lane is required;
- producing one durable `INVESTIGATION HANDOFF` for downstream specialists with exact provenance references.

Does not own:

- production implementation by default;
- creating or updating the delivery PR by default;
- weakening existing tests to fit a hypothesis;
- product/UX decisions that belong to Designer;
- independent final technical acceptance;
- Kanban lifecycle/resource reconciliation;
- waiting for future CI, human review, merge, or comments.

The Investigator handoff is the Developer's **primary task context**, not a replacement for source-of-truth access. It must cite exact Issue/PR/commit/path/head identities so downstream workers can perform bounded verification without rereading the whole history by default.

For ordinary implementation, the handoff should include at least objective, scope/non-goals, ownership boundary, acceptance criteria, relevant files/contracts, validation gates, design-required decision, and source provenance.

For regression/rework, it should additionally include when applicable:

- observed failure and confirmed working boundaries;
- first failing boundary;
- last known-good / known-bad evidence;
- regression window and suspicious changes;
- primary root-cause hypothesis with confidence;
- why existing tests or prior fixes missed the failure;
- deterministic RED regression required before implementation is considered fixed;
- preserved behavior/contracts;
- rejected workaround classes;
- unknowns that still require runtime evidence.

Investigator must not report a hypothesis as confirmed cause merely because it fits the current implementation. A passing unit/E2E test does not override a reproducible runtime failure.

Stop rule:

Once the bounded investigation and structured handoff are complete, Investigator finishes. It does not remain RUNNING while Developer works and does not mutate downstream lifecycle state.

## 4. Hermes Kanban Developer

Developer owns implementation delivery **from the approved investigation/design handoff**.

Owns:

- reading the Investigator handoff and repository-owned instructions/contracts needed to implement it;
- bounded source-of-truth verification when the handoff references a specific file, commit, PR head, or contract;
- coding/debugging/refactoring/configuration inside the assigned implementation boundary;
- making the required RED regression fail pre-fix and pass post-fix when the handoff requires one and the environment can execute it;
- required repository-local deterministic validation;
- final diff inspection;
- creating or updating the required GitHub PR;
- reporting exact branch/PR/head, tests run, unavailable gates, and remaining risks.

Does not own:

- reconstructing the entire source Issue/PR/history from scratch by default;
- silently replacing the Investigator's root-cause model with an unrelated workaround without returning the contradiction as evidence;
- product/UX decisions when materially ambiguous;
- final independent acceptance;
- Kanban lifecycle reconciliation;
- waiting for future CI, human review, merge, or comments.

If fresh repository evidence materially contradicts the handoff, Developer must record the contradiction and stop or return bounded evidence for re-investigation rather than papering over the mismatch. Developer may perform a narrow lookup needed to implement safely; it should not duplicate the whole Investigator phase merely because source access is available.

Stop rule:

Once the assigned implementation is delivered, required local validation has actually run, and the PR is created/updated, Developer records the **current** external state once and hands off. Pending future CI/review/merge is a handoff fact, not a reason to stay RUNNING.

`NOT RUN` is never `PASS`. If an external/manual gate cannot run now, record it honestly without turning the worker into a monitor.

## 5. Hermes Kanban Reviewer

Reviewer is independent technical verification. Investigator and Developer handoffs are evidence inputs, not conclusions the Reviewer must accept.

Owns:

- identifying the exact PR/head under review;
- inspecting the actual diff and relevant source;
- checking repository/task contracts and the Investigator handoff;
- verifying that the implemented change addresses the observed failure/ownership boundary rather than only the proposed mechanism;
- checking the required RED regression and preserved contracts when applicable;
- validating upstream evidence;
- running the smallest useful deterministic checks when needed;
- returning one verdict: `PASS` or `REWORK`.

Does not own:

- implementing fixes by default;
- recreating the full investigation phase unless new evidence invalidates the handoff;
- creating downstream rework tasks or manipulating task dependencies;
- product aesthetics when a design lane exists;
- waiting for future CI/review/merge events;
- lifecycle/resource reconciliation.

Reviewer evaluates the review surface that exists now. When returning a `REWORK` verdict, Reviewer provides a structured handoff (exact evidence, findings, inspected head, and required validation) and finishes its run. Reviewer must not create child rework tasks. A future external/manual gate may be reported separately, but the reviewer must not remain alive solely to poll for it.

When Reviewer discovers that the implementation failed because the investigation model itself is incomplete or contradicted by runtime evidence, the verdict must say so explicitly. Main then routes a **fresh Investigator phase before another Developer round** rather than sending the same stale model directly back to Developer.

## 6. Hermes Kanban Designer

Designer owns user-facing product/UX decisions and design review. Designer consumes the Investigator handoff as its technical/problem context instead of independently reconstructing Issue/PR history unless a bounded source check is needed.

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

Designer does not become the default software implementer, regression investigator, technical reviewer, or lifecycle controller.

## Investigator handoff invariant

For GitHub-backed implementation/rework that uses the specialist graph:

1. Investigator completes before Developer becomes dependency-ready.
2. Developer task context references the Investigator task/handoff and exact source provenance rather than copying an unbounded raw Issue/PR transcript.
3. The handoff does not hide source-of-truth identity: Issue/PR URLs, current head SHA, relevant commits, repository paths, and unresolved unknowns remain explicit.
4. Developer may verify bounded facts against source, but wholesale history reconstruction is not the default implementation responsibility.
5. Reviewer independently verifies the current implementation and may invalidate the investigation model when fresh evidence warrants it.
6. Main owns deciding whether a Reviewer `REWORK` returns directly to Developer (clear local implementation defect) or must reopen Investigator first (root-cause uncertainty, runtime contradiction, failed prior hypothesis, or repeated rework).

## Rework graph invariant

When a reviewer reports `REWORK`:

1. **Structured handoff only**: The reviewer returns a structured report containing verdict=`REWORK`, exact path:line findings, inspected PR head SHA, and required validation commands. The reviewer terminates its run without creating child tasks.
2. **Main Agent owns graph recreation**: Only the Main Agent creates the next bounded specialist graph.
3. **Local implementation defect**: when the root-cause model remains valid and the defect is a clearly bounded implementation mistake, Main may create `bounded developer rework -> fresh reviewer` using the existing Investigator handoff plus Reviewer findings.
4. **Investigation-invalidating rework**: when runtime evidence contradicts automated tests, a prior root-cause hypothesis failed, the failure boundary is still unknown, or another Developer round would begin by rediscovering history, Main creates:
   ```text
   prior implementation / source task (done)
     -> fresh investigator (todo -> ready)
          -> bounded developer rework (todo)
               -> fresh reviewer (todo)
   ```
5. **No non-terminal parent dependencies**: Never set a blocked, review-waiting, or non-terminal reviewer task as the blocking parent (`parents=[t_reviewer]`) of the Developer/Investigator rework task. Doing so causes an immediate `parents_not_done` deadlock.
6. **Attach review context by reference**: reference the prior reviewer task ID/findings in the new Investigator or Developer body/comments/metadata rather than creating a blocking dependency from a non-terminal reviewer.
7. **No forced promotion loops**: Never use repeated `promote --force` to bypass `parents_not_done`. Repair dependency topology using canonical Kanban/control-plane dependency operations and reserve direct database interventions exclusively for explicit manual operator recovery.

### Internal `REWORK` verdict is not the GitHub `agent-rework` command

The specialist verdict `REWORK` above is an **internal Kanban review result**. It authorizes Main to recreate the bounded specialist graph described here; it does not authorize any worker, reviewer, investigator, or controller to create, restore, or infer the PR label `agent-rework`.

The PR-side `agent-rework` label is a separate trusted maintainer one-shot control-plane command owned by the GitHub ↔ Kanban edge lifecycle in `EDGE_REWORK_LIFECYCLE.md`. Internal specialist output must never be translated into that label merely because both use the word “rework”.

## GitHub-backed lifecycle invariant

For an Issue-backed root card with a linked PR:

1. specialist investigation/design/implementation/review work completes through Kanban dependencies, and the current bounded graph's terminal specialist is a verified direct parent of the intake root before Main yields;
2. no specialist remains RUNNING solely to wait for GitHub Actions, human review, merge, or future comments;
3. the root worker terminates with core `kanban_complete` only when its required internal graph is satisfied and the current terminal root parent is terminal;
4. that core `done` is provisional and is not GitHub merge evidence. Main Agent must not declare work "merged" or "delivered" based on internal graph completion;
5. edge reconciliation projects an OPEN or closed-unmerged required PR to parked `review` and clears worker ownership;
6. only trusted PR-side rework admission may make the GitHub-backed card runnable again; an internal specialist `REWORK` verdict does not synthesize that GitHub signal;
7. fresh GitHub evidence that every required PR merged into the target branch permits authoritative `done`.

This separation keeps scarce worker slots tied to active work rather than external waiting.
