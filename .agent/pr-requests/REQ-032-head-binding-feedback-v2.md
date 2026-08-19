# REQ-032 v2: rework head-binding rejection feedback

- Status: direct implementation in PR #39; Hermes intake intentionally unused
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Integration target: latest `main` after PR #36
- Source issue: #32
- Supersedes: closed/unmerged PR #33
- Merge authority: human/user only

## Objective

Keep PR #33's useful behavior — visible, idempotent feedback for head-binding delivery rejection — while preserving PR #36's verification-only same-head retry semantics.

## Current contract

- Ordinary `rework_head_unchanged` rejection gets one idempotent PR comment that names the round-requested head and explains the required bounded head advance.
- `run_head_mismatch` gets attestation-specific feedback: marker head, live PR head, and the current finished worker-run head must bind; another source commit is not inherently required.
- PR #36's trusted-maintainer verification-only same-head success path remains authoritative and must produce no head-binding warning.
- Feedback is observational only: retry/state/label/failure-limit routing is unchanged, and comment failure must not abort reconciliation.

## Implementation shape

The large canonical `edge/kanban-github-sync.py` state machine stays unchanged. `edge/kanban_head_binding_feedback.py` is a narrow overlay installed by `edge/kanban-github-sync-entrypoint.py` after the canonical core loads. The deploy script candidate-compiles, atomically installs, and hash-verifies the overlay with the existing edge dependencies.

`edge/test-kanban-head-binding-feedback.py` exercises the two rejection paths and the verification-only no-warning path, then runs the complete pre-existing rework harness with the production overlay installed.
