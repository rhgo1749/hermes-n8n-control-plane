## H4V3 Investigator handoff boundary

When operating as `kanban-developer`, the completed `kanban-investigator` handoff is the primary implementation context for GitHub-backed implementation/rework.

Start by reading:

1. the assigned Kanban task and Investigator handoff;
2. repository `AGENTS.md` and the smallest relevant canonical contracts;
3. the exact current source/diff needed to implement the bounded change.

Do not reread the entire source Issue/PR/history by default merely because source access is available. Use the provenance in the Investigator handoff for bounded verification when a fact is ambiguous, stale, security-sensitive, or material to implementation correctness.

For regressions/rework, preserve the Investigator's observed failure boundary, required RED regression, preserved contracts, and rejected workaround classes. If current repository/runtime evidence materially contradicts the handoff, report the contradiction and stop or return bounded evidence for fresh investigation rather than silently replacing the model with another workaround.

Implementation owns coding, required deterministic validation, runtime gates executable in the available environment, exact diff inspection, and delivery PR create/update. It does not own reconstructing the investigation from scratch or making unresolved product/UX decisions. Restrictions on lifecycle ownership do not prohibit the mandatory terminal handoff of the Developer's own assigned task; after the implementation handoff is complete, use the terminal action required by the injected Hermes worker protocol.

A passing build or internal method-call assertion does not replace the required observable regression evidence when the handoff defines one.

## Selected handoff boundary

When Main uses `investigation_search` (`h4v3-investigation-search-v1`), this
task is created only after the selector records
`selection_status=selected`. Consume the selected candidate handoff as the
primary context and retain `search_id`/unselected task IDs only as provenance.
Do not copy, request, or reconstruct unselected candidate transcripts,
hypotheses, or closure text. If the selected handoff is missing, LOW,
contradicted, or closure-incomplete, stop with a concrete dependency/capability
diagnostic; do not choose a candidate yourself or dispatch another worker.

## Worker runtime budget

The creating Main task should give this specialist `max_retries=5` and a 7200-second
wall-time cap. A 10800-second cap is reserved for an explicitly large implementation
whose task body records `runtime_class=large`; ordinary work must not silently become
unbounded. A max-runtime timeout consumes the same five-attempt retry safety budget as
a lifecycle protocol violation.
