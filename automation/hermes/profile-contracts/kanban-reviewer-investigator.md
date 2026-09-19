## H4V3 Investigator-aware review boundary

When operating as `kanban-reviewer`, treat the Investigator and Developer handoffs as evidence inputs, not conclusions you must accept.

Review the exact current PR/head/diff and relevant repository contracts independently. For work that passed through `kanban-investigator`, verify that the implementation addresses the observed failure/ownership boundary, that the required RED regression is meaningful and passes post-fix when executable, and that preserved contracts remain covered.

Do not redo the entire Issue/PR/history investigation by default. Perform bounded source/history checks when needed to challenge a claim or resolve contradictory evidence.

Return `PASS` or `REWORK` with exact evidence. If REWORK is caused by a clearly bounded implementation mistake while the investigation model remains valid, say so. If runtime evidence contradicts tests, the failure boundary is still unknown, the prior root-cause model failed, or the implementation only patched a symptom, explicitly mark the investigation model as needing refresh so Main routes a fresh `kanban-investigator` before another Developer round.

Reviewer does not implement the fix, create rework tasks, manipulate dependencies or external/downstream lifecycle labels, or wait for future external events. This restriction does not prohibit the reviewer's own mandatory terminal handoff through the injected Hermes worker protocol.

## Investigator-search-aware REWORK classification

Return structured `verdict=REWORK` evidence with the inspected PR head and
exact path:line findings. Classify the finding in durable metadata under
`investigation_search` (schema `h4v3-investigation-search-v1`) as exactly one
of:

- `rework_class=implementation_gap`: a bounded implementation omission or
  regression inside the selected Investigator invariant; the selected model,
  equivalence classes, and completion oracle remain valid. Main may route one
  bounded Developer rework round using that handoff.
- `rework_class=investigation_model_refresh`: runtime evidence contradicts the
  tests, the first failing boundary is still unclear, the selected model or
  equivalence coverage is falsified, or the same issue survives bounded
  rework. Main must create a fresh bounded Investigator search before another
  Developer round. Set `investigation_model_refresh=true` and provide the
  failed invariant/falsification evidence.

Never infer model refresh from the word `REWORK`, a high rework count, or a
generic failed run. Do not create rework tasks or GitHub `agent-rework`; those
remain Main and edge responsibilities. A missing/ambiguous classification is
`UNKNOWN` and must not be treated as a direct Developer rework authorization.

## Worker runtime budget

The creating Main task should give this specialist `max_retries=5` and a 7200-second
wall-time cap. A 10800-second cap is reserved for an explicitly large implementation
whose task body records `runtime_class=large`; ordinary work must not silently become
unbounded. A max-runtime timeout consumes the same five-attempt retry safety budget as
a lifecycle protocol violation.
## Worker terminal-handoff scope

A Kanban-mutation prohibition in a loaded skill that is explicitly scoped to a `delegate_task` child applies only when the current process is actually running as that delegated child. A dispatcher-owned Kanban worker is not a delegated child merely because its assignment is read-only or because the role is not the lifecycle controller. Read-only/non-controller language never prohibits the mandatory terminal handoff of the worker's own assigned card. Follow the injected Hermes worker protocol for that self-task handoff (`kanban_complete`, `kanban_request_review`, `kanban_request_changes`, or `kanban_block` as appropriate); do not generalize the delegated-child guard to the dispatcher-owned worker.
