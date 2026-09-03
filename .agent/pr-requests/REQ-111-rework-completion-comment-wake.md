# REQ-111: AGENT_REWORK_COMPLETE issue_comment edge wake hotfix

- Status: proposed
- Project: hermes-n8n-control-plane
- Source issue: #111
- Base branch: `main`
- Required work branch: `hotfix/111-rework-completion-comment-wake`
- Merge authority: Human/user only

## Objective

A managed repository PR conversation `issue_comment(created)` whose first non-empty line is exactly `AGENT_REWORK_COMPLETE` must wake the existing GitHub/Kanban edge reconciler immediately, while preserving the canonical edge as the only completion authority.

## Confirmed incident

CtrlHangul PR #92 posted a valid verification-only completion handoff for canonical task `t_1187aba8`, but the task remained durable `done` and the PR retained stale `agent-working`. A task-scoped live dry-run immediately predicted `agent_review_ready`, proving that the edge state machine was already correct and the missing boundary was event wake routing.

## Safety contract

- Keep signed webhook verification, delivery dedupe, owner scope, managed-repository admission, onboarding and durable generic scope behavior in the canonical router.
- The completion marker is only a low-latency wake hint. Never trust it as task/head/actor/validation authority.
- Existing edge fresh-read verification remains authoritative for trusted actor, task/request identity, live PR head, validation, timing and current-round run provenance.
- Ordinary Issue comments, ordinary PR discussion, edited/deleted comments and marker text that is not the first non-empty line retain the generic intake path only.
- Unknown repositories must finish onboarding before using the low-latency lane.
- A failed low-latency hop must remain retryable; do not swallow the failure after the generic scope was persisted.

## Implementation

Use a small router entrypoint overlay rather than rewriting the large canonical router. The overlay observes the already-parsed signed event, persists the ordinary repo scope through the canonical implementation, and for one admitted managed-repository completion-comment hint invokes the existing n8n/actuator edge wake envelope. That envelope is control-plane-only; the edge immediately fresh-reads GitHub and does not use the envelope as PR state evidence.

## Acceptance

- focused completion-comment wake tests pass;
- existing router/startup/networking/validation tests pass;
- current CtrlHangul `t_1187aba8` converges `done -> review` and PR #92 converges `agent-working -> agent-review-ready` after scoped reconciliation;
- public signed-webhook completion-comment fixture reaches the edge wake owner after deployment;
- existing merged-PR and `agent-rework` low-latency wakes remain unchanged;
- no automatic merge.
