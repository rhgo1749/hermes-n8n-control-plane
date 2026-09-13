# REQ-138: invalid ready implementation/REWORK card preflight

- Status: Implementation complete / review pending
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `HERMES_PLUGIN`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `N8N_VALIDATE`, `HERMES_PLUGIN`
- Source Issue: `rhgo1749/hermes-n8n-control-plane#138`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/138`
- Source-of-truth base: `origin/main` at `e8ee13718f8eb5b067f0d8743b1a73ecd43517f8`
- Root Kanban task: `t_7ec21f55`
- Investigator handoff: `t_19eff282` (durable handoff comment `#450`)
- Current bounded Developer task: `t_6407c785`
- Downstream Reviewer task: `t_7dd12b01`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:138`
- Controller workspace binding: `/ws/projects/hermes-n8n-control-plane/.worktrees/t_6407c785`
- Delivery branch: `fix/issue-138-ready-binding-preflight`
- Delivery PR: PR #149 — `Issue #138: 구현·재작업 카드 생성 전 작업공간 바인딩 검증` — `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/149`
- Merge/auto-merge authority: human/user only
- Automation stop state: `NONE`; no live deployment or config mutation was performed in this round.

## Objective and confirmed cause

At the pre-round PR #149 head `37237824fe57a7247eed830d30f276197a67785b`, the shared parser in `automation/hermes/scripts/kanban-specialist-completion-guard.py` skipped executable command substitutions. Double-quoted and backtick `$()` bodies, including supported nested `bash -lc`/`env bash -lc` forms, therefore produced no specialist invocation and could reach the real shell with rc=0.

The bounded fix keeps one parser owner and lexically records `$()`/backtick bodies with malformed status and command-chain segment ownership. Reachable bodies are recursively inspected through the existing invocation walker for specialist `create`, `assign`, or `reassign`; single-quoted/escaped data and harmless substitutions remain inert, malformed or over-deep relevant bodies fail closed, and no predicate is executed. The earlier grouping/operator rejection and literal deterministic `false &&` / `true ||` / `true &&` / `false ||` behavior remain unchanged.

## Scope and non-goals

In scope for this round:

1. `automation/hermes/scripts/kanban-specialist-completion-guard.py`: fail-closed detection of executable `$()` and backtick bodies, including supported nested shell wrappers and bounded malformed handling.
2. Parser regressions for double-quoted/unquoted substitutions and backticks across `create`, `assign`, and `reassign`, plus nested wrappers, short-circuit, malformed/depth, grouping/operator, literal, escaped, and harmless controls.
3. Stable `kanban-block-kind-guard.py` wrapper and workspace-binding no-mutation regressions proving rc=2, diagnostics, byte-identical board state, zero rows/adapter activity, fake-shell zero invocation, and no assignment/reassignment.
4. This tracked REQ provenance refresh.

Explicit non-goals: Hermes core or product changes; a second parser, dispatcher, or state store; arbitrary shell interpretation/predicate execution; GitHub lifecycle changes; cron/polling; live deployment/config mutation; active-worker rebinding; new PR creation; merge/auto-merge; hosted Actions; unrelated stale-test cleanup.

## Changed-file allowlist

Current round edits (plus this REQ document):

- `automation/hermes/scripts/kanban-specialist-completion-guard.py`
- `tests/test_specialist_completion_contract_parser.py`
- `tests/test_specialist_completion_contract_guard.py`
- `tests/test_kanban_workspace_binding_guard.py`
- `.agent/pr-requests/REQ-138-reject-invalid-ready-implementation-cards.md`

Cumulative PR #149 allowlist relative to `origin/main`:

- `.agent/REQ_REQUEST_TEMPLATE.md`
- `.agent/pr-requests/REQ-138-reject-invalid-ready-implementation-cards.md`
- `automation/hermes/profile-contracts/README.md`
- `automation/hermes/profile-contracts/kanban-main-investigator.md`
- `automation/hermes/scripts/deploy-intake-edge.sh`
- `automation/hermes/scripts/kanban-block-kind-guard.py`
- `automation/hermes/scripts/kanban-block-kind-hook-config.py`
- `automation/hermes/scripts/kanban-specialist-completion-guard.py`
- `automation/hermes/scripts/kanban-workspace-binding-guard.py`
- `docs/KANBAN_ROLE_CONTRACTS.md`
- `tests/test_kanban_investigator_role.py`
- `tests/test_kanban_workspace_binding_guard.py`
- `tests/test_specialist_completion_contract_guard.py`
- `tests/test_specialist_completion_contract_parser.py`

No Hermes core, product repository, n8n workflow ownership, cron, or live runtime file was changed in this round.

## Validation evidence

- Expected RED against a clean `3723782` parser copy: `22 failed, 32 passed` in the current parser regression file; the failures are the new executable-substitution, nested-wrapper, short-circuit, malformed, depth, and operator assertions. This proves the exact regressions bite before the fix.
- Focused current-round suite after the fix: `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_specialist_completion_contract_parser.py tests/test_specialist_completion_contract_guard.py tests/test_kanban_workspace_binding_guard.py` → `218 passed`.
- Focused substitution/short-circuit/group/operator workspace selection: `57 passed, 58 deselected`; fake-shell stable-hook cases assert rc=2, no shell invocation, unchanged DB bytes, and no task/assignment activity.
- Real-core parser/handler probes: atomic `barrier`, `rollback`, `paths`, `same-key`, `lifecycle`, and `duplicates` scenarios PASS; handler `padded`, `cli-surface`, and `shell` scenarios PASS.
- Direct edge scripts: workspace admission `45 passed`; workspace self-heal `38 passed`; race gate `5/5` in each of two iterations; completion, dependency, and terminal-convergence gates PASS (`95 passed` terminal convergence).
- Rework/projection suites: delivery provenance `9 passed`; attention delivery recovery `8 passed`; edge projection/label history `4 passed`; parking-comment `20 passed`; GitHub-sync rework `925 passed`.
- Block-kind suite on candidate: `60 passed, 2 failed`; both failures are legacy deployer dry-run fixtures that omit required H4V3 profile configs (fresh `origin/main` baseline is `62 passed`). No block-kind production file changed in this round.
- Full repository candidate vs fresh `origin/main`: candidate `589 passed, 4 failed, 23 errors`; baseline `394 passed, 4 failed, 23 errors`. The same four completion-wake failures and 23 board-identity fixture setup errors occurred on both trees; no candidate-only failure/error identity was observed.
- N8N validator: `python3 automation/n8n/scripts/validate.py` → `{"ok": true, "schedule_workflows": 0, "edge_sync_workflows": 1, "github_workflows": 1, "github_event_router": 1, "edge_sync_execution": "n8n-webhook->direct-actuator:5682", "hermes_cron_required": false, "hermes_schedule_owned_by_n8n": false}`.
- Static checks: changed-file `python3 -m py_compile` PASS; Ruff `E4,E7,E9,F` PASS (`All checks passed!`); basedpyright `--level error` PASS (`0 errors, 0 warnings, 0 notes`); profile `lsp/bin/pyright-langserver` severity-1 diagnostics PASS (`0` diagnostics; 7 publish messages); `bash -n` and ShellCheck PASS for the existing Hermes deploy/install scripts; `git diff --check` PASS.
- Auxiliary canonical tests: `test_n8n_import_contract.py`, `test_github_router.py`, `test_github_intake_actuator.py`, `test_intake_completion_contract_entrypoint.py`, completion edge wake, intake lease, repository-scope, registry, and registry-workdir suites PASS. `test_github_event_concurrency_contract.py` remains a pre-existing compose-contract assertion failure (`origin/main` uses `${GITHUB_ROUTER_INSTALLATION_ID:-}` while the test expects `:?`); `test_cutover_snapshot_boundary.py` remains a pre-existing temporary-fixture failure because `state-root.sh` is absent. Neither path changed.
- GitHub Actions are disabled by repository policy and were not used as a substitute for local validation.

## Delivery and rollback boundary

- Implementation checkpoint: `d5b14149e7d2a2471953002ea11cb6c24e6e7d9c` (`fix(kanban): reject specialist command substitutions`). The subsequent REQ-only provenance commit will be the final publication commit for this round.
- Stable live entrypoint identity remains the repository-approved `automation/hermes/scripts/kanban-block-kind-guard.py`; it delegates terminal classification to the shared specialist parser and then to the existing workspace-binding guard for native creation. This round did not deploy or mutate that live hook/config.
- Rollback boundary is the existing PR #149 branch: revert the substitution-parser commit and this REQ-only provenance commit as ordinary branch commits; no Hermes core rollback or live host operation is required for this round.
- PR #149 will be fresh-read after both publication commits as OPEN/unmerged, base `main`, branch `fix/issue-138-ready-binding-preflight`, with exactly one Issue #138-linked PR and visible plain-text `Closes #138.`. The exact final head is authoritative only from the post-push GitHub/remote read-back and Kanban handoff, not from a self-referential SHA embedded in this file.
- Merge/auto-merge was not performed. Internal Developer completion remains provisional until the independent Reviewer and canonical edge reconciliation complete.

Closes #138.
