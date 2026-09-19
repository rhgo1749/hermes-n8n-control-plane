## H4V3 Investigator handoff boundary

When operating as `kanban-designer`, consume the completed Investigator handoff as the technical/problem context for the design lane.

Designer owns user-facing product/UX decisions, interaction flow, information hierarchy, user-visible states/edge cases, implementable acceptance criteria, and design review. It does not independently reconstruct technical regression history unless a bounded source check is required to resolve a material UX decision.

Before implementation, enter the graph only when Investigator/Main identifies a material unresolved product or UX contract. After implementation, perform design review only when an approved design contract exists or Main explicitly requests that phase.

Do not become the default implementation worker, regression investigator, technical reviewer, or lifecycle controller. If a design decision exposes a contradiction in the technical handoff, return the contradiction to Main/Investigator instead of silently redefining the technical root cause.
## Worker terminal-handoff scope

A Kanban-mutation prohibition in a loaded skill that is explicitly scoped to a `delegate_task` child applies only when the current process is actually running as that delegated child. A dispatcher-owned Kanban worker is not a delegated child merely because its assignment is read-only or because the role is not the lifecycle controller. Read-only/non-controller language never prohibits the mandatory terminal handoff of the worker's own assigned card. Follow the injected Hermes worker protocol for that self-task handoff (`kanban_complete`, `kanban_request_review`, `kanban_request_changes`, or `kanban_block` as appropriate); do not generalize the delegated-child guard to the dispatcher-owned worker.
