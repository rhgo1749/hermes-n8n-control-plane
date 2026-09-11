## H4V3 Investigator handoff boundary

When operating as `kanban-developer`, the completed `kanban-investigator` handoff is the primary implementation context for GitHub-backed implementation/rework.

Start by reading:

1. the assigned Kanban task and Investigator handoff;
2. repository `AGENTS.md` and the smallest relevant canonical contracts;
3. the exact current source/diff needed to implement the bounded change.

Do not reread the entire source Issue/PR/history by default merely because source access is available. Use the provenance in the Investigator handoff for bounded verification when a fact is ambiguous, stale, security-sensitive, or material to implementation correctness.

For regressions/rework, preserve the Investigator's observed failure boundary, required RED regression, preserved contracts, and rejected workaround classes. If current repository/runtime evidence materially contradicts the handoff, report the contradiction and stop or return bounded evidence for fresh investigation rather than silently replacing the model with another workaround.

Implementation owns coding, required deterministic validation, runtime gates executable in the available environment, exact diff inspection, and delivery PR create/update. It does not own reconstructing the investigation from scratch or making unresolved product/UX decisions.

A passing build or internal method-call assertion does not replace the required observable regression evidence when the handoff defines one.
