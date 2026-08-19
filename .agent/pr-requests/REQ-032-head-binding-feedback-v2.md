# REQ-032 v2: rework head-binding rejection feedback

- Status: superseded by Issue #32 agent-ready intake
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Integration target: latest `main` after PR #36
- Source issue: #32
- Supersedes: closed/unmerged PR #33
- Merge authority: human/user only

## Objective

Keep PR #33's useful behavior — visible, idempotent feedback for head-binding delivery rejection — while preserving PR #36's verification-only same-head retry semantics.

The implementation source of truth is now Issue #32. This draft branch is retained only as historical handoff; the Issue is re-intaked with `agent-ready` so Hermes creates the canonical Kanban task before any implementation PR exists.
