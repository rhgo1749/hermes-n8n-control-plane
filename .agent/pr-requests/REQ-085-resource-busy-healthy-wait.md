# REQ-085: resource-busy READY 대기와 worker reclaim health 통합

- Status: Rework round 1 implementation complete; review handoff pending
- Project: `hermes-n8n-control-plane`
- Product type: `HERMES_PLUGIN` / `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `HERMES_PLUGIN`
- Integration target branch: `main`
- Required work branch: `issue85/resource-busy-healthy-wait`
- Source-of-truth base: `origin/main` at `dd7f6ad6c6fe39108d87a821c635046ab1fb88e1`
- Source issue: `rhgo1749/hermes-n8n-control-plane#85`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/85
- Kanban implementation task: `t_9378b599`
- Kanban rework task: `t_89a3c0a8`
- Intake root provenance: `t_7494ffd8`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:85`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Pull request title/body/final report language: Korean
- Merge authority: Human/user only
- Automation stop state: `NONE` (local implementation gates complete; PR review/merge remain external)

## Objective

Keep capacity backpressure out of the dispatcher-stuck health signal while
preserving the existing Hermes core state machine. The enabled resource plugin
must expose bounded `resource_busy` diagnostics for normal READY and autonomous
REVIEW claims, safely release/reclaim dead or terminal workers, and make
`dispatch --dry-run` report capacity backpressure instead of a predicted spawn.

## Confirmed boundary

- `edge/kanban_resource_admission.py` already gates the edge rework lane and
  returns the core claim sentinel when a configured resource is full.
- The plugin runtime loads edge helpers and wraps core claim functions; Hermes
  core source is read-only for this request.
- Core `has_spawnable_ready` only knows READY/profile eligibility, so a full
  resource was previously classified as spawnable by the gateway health probe.
- Core dry-run intentionally skips the live claim wrapper and therefore needed
  a read-only resource classification overlay.

## In scope

1. Runtime monkeypatches from the enabled plugin for `claim_task`,
   `claim_review_task`, health probes, dispatch results, and CLI dry-run output.
2. Bounded in-process admission diagnostics shared by core and edge paths;
   verified terminal same-task workers may be reaped, while live/unverifiable
   workers and PID reuse remain fail-closed.
3. Regression coverage for capacity-1 READY/REVIEW waits, health
   classification, release/reclaim, dry-run filtering, mixed queues, bounded
   diagnostics, and terminal/active/PID-reuse safety.
4. Dry-run virtual reservations, health-visible configuration/inspection
   failures, and same-tick stale `resource_busy` normalization.
5. Canonical `docs/EDGE_WORKER_RESOURCE_ADMISSION.md` update.

## Explicit non-goals

- No changes under `/ws/hermes-agent/hermes_cli/` or any Hermes core source.
- No capacity increase/removal, second dispatcher, polling cron, manual DB
  repair, arbitrary PID killer, or new state/notification database.
- No changes to cloud-parallel, no-resource, unmatched-profile, endpoint-pool
  (#55), GitHub lifecycle, n8n, Overview, or Telegram ownership.
- No merge or auto-merge.

## Acceptance/test mapping

- Capacity-1 full resource leaves READY/REVIEW unclaimed and emits bounded
  `resource_busy`: focused health test.
- All-busy queues are false from the patched health probes; mixed queues remain
  spawnable: focused health test.
- Dead holder releases capacity; subsequent claims become RUNNING: focused
  health test.
- Dry-run remains read-only, omits busy candidates from `spawned`, and exposes
  `resource_busy`; same-resource candidates consume virtual capacity in order:
  focused health + CLI formatter tests.
- Policy-resolution and active-worker inspection failures keep health
  spawnable/visible and record explicit diagnostics: focused health test.
- A pre-tick busy candidate that is spawned after same-task terminal-worker reap
  appears only in spawn evidence: focused health test.
- Terminal live, active live, and PID-reuse paths preserve no-duplicate safety:
  focused same-task safety test plus existing resource-admission suite.
- Legacy/backend/cloud and reservation behavior remains unchanged: existing
  resource-admission and dynamic-resource suites.
- Full GitHub edge reconciliation remains green: existing EDGE_REWORK suite.

## Validation record

- Implementation commit validated: `641310b5abe0a590072809b4891d5f9f832c4ed0`
- `python3 edge/test-kanban-resource-busy-health.py` — PASS (35 checks)
- `python3 edge/test-kanban-resource-admission.py` — PASS (18 checks)
- `python3 edge/test-kanban-dynamic-resource.py` — PASS (10 checks)
- `/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py` —
  PASS (769 checks at the final head)
- `python3 -m py_compile edge/kanban_resource_admission.py
  edge/kanban_dynamic_resource.py edge/test-kanban-resource-busy-health.py` — PASS
- `PYTHONPATH=/ws/hermes-agent /home/hermes/.hermes/profiles/kanban-main/lsp/node_modules/.bin/pyright
  edge/kanban_dynamic_resource.py edge/test-kanban-resource-busy-health.py` —
  PASS (0 errors, 0 warnings, 0 informations)
- `ruff check --select I edge/kanban_dynamic_resource.py
  edge/test-kanban-resource-busy-health.py` — PASS
- `git diff --check` — PASS
- `bash -n automation/hermes/scripts/deploy-intake-edge.sh` plus isolated
  temporary-target `--dry-run` — PASS
- Sabotage: the pre-fix source at `838e336f4bb863ae303a82a3b1baac6145a735ba`
  with the new focused regressions exited non-zero as expected (29 passed, 6
  failed); virtual reservation, health failure visibility, and stale busy
  normalization checks each failed.
- GitHub Actions: intentionally disabled by repository policy; local gates are
  authoritative.

## Delivery contract

Create exactly one PR from `issue85/resource-busy-healthy-wait` into `main`.
The PR body must contain the plain-text line `Closes #85.` outside code fences.
Verify the final PR head SHA, REST read-back, and GraphQL
`PullRequest.closingIssuesReferences`; do not merge or enable auto-merge.
