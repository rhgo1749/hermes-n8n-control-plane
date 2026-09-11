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

Developer task context should reference the completed Investigator task/handoff and retain exact source provenance. Raw transcripts are not the handoff.

Investigator may be omitted only for a genuinely mechanical task with no meaningful Issue/PR/history synthesis and no implementation decision that investigation could change. Never omit it for regressions, existing-PR rework, runtime-vs-test mismatch, a failed prior root-cause hypothesis, or repeated implementation rounds.

If Reviewer returns REWORK because of a clear local implementation mistake while the root-cause model remains valid, Main may send bounded rework directly to Developer using the existing Investigator handoff. If runtime evidence contradicts tests, the failure boundary remains unclear, the prior hypothesis failed, or the same problem survives rework, create a fresh Investigator phase before another Developer round.

All H4V3 specialist tasks including `kanban-investigator` use `completion_contract=local-only` (or omit it so Hermes defaults to local-only). GitHub acceptance/merge remains root-card edge authority.
