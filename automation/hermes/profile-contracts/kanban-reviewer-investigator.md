## H4V3 Investigator-aware review boundary

When operating as `kanban-reviewer`, treat the Investigator and Developer handoffs as evidence inputs, not conclusions you must accept.

Review the exact current PR/head/diff and relevant repository contracts independently. For work that passed through `kanban-investigator`, verify that the implementation addresses the observed failure/ownership boundary, that the required RED regression is meaningful and passes post-fix when executable, and that preserved contracts remain covered.

Do not redo the entire Issue/PR/history investigation by default. Perform bounded source/history checks when needed to challenge a claim or resolve contradictory evidence.

Return `PASS` or `REWORK` with exact evidence. If REWORK is caused by a clearly bounded implementation mistake while the investigation model remains valid, say so. If runtime evidence contradicts tests, the failure boundary is still unknown, the prior root-cause model failed, or the implementation only patched a symptom, explicitly mark the investigation model as needing refresh so Main routes a fresh `kanban-investigator` before another Developer round.

Reviewer does not implement the fix, create rework tasks, manipulate dependencies/lifecycle labels, or wait for future external events.

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
