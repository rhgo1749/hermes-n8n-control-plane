# REQ-104 — semantic operator-attention identity

Source Issue: #104
Kanban task: t_6b2a4238
Work branch: issue104-operator-attention-incident-identity
Base: origin/main (fetched before implementation)

## Scope

Replace event-id-cursor operator-attention dedupe with a stable semantic identity:

- Edge `github_operator_attention` events use `attention_key = reason:incident_ref`.
- Rework attention identity is repository, Issue, PR, rework round, and request-comment identity.
- Blocked/human-input identity is the latest `blocked` event payload kind/reason/timestamp plus task `block_kind`.
- Other diagnostics use the canonical entry PR identity when available.
- Provenance is persisted with the event; missing identity is fail-open and marked `incident_unresolved`.
- Overview preserves semantic rows across ordinary event churn, retires them when the governing blocked/rework incident changes or resolves, and keeps the legacy cursor fallback for rows without provenance.
- Telegram notification lines carry the same semantic key so the existing last-sent body dedupe re-arms for a new generation.

## Explicit non-goals

Do not modify Hermes core, introduce a second notification store, use a raw task-event cursor as incident identity, change Kanban state-machine ownership, alter Telegram credentials/configuration, or merge/auto-merge a PR.

## Validation profiles

- Focused edge, intake-notification, Overview, and regression tests.
- Python compile/import checks and repository diff hygiene.
- Static diagnostics are reported separately from tests; pre-existing repository-wide lint findings are not treated as introduced changes.

## Stop conditions

Stop if canonical blocked/rework identity conflicts, required source ownership is ambiguous, or the change would require core/state migration outside this repository. GitHub Actions are disabled by repository policy; local gates are authoritative and remote CI is reported separately.

## Final automation state

Implementation evidence is handed off after local validation, commit, push, and PR verification. Human review, merge, and device/manual acceptance remain outside this request.
