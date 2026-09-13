# REQ-138: invalid ready implementation/REWORK card preflight

- Status: Implementation published / review pending
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `HERMES_PLUGIN`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `N8N_VALIDATE`, `HERMES_PLUGIN`
- Source Issue: `rhgo1749/hermes-n8n-control-plane#138`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/138`
- Source-of-truth base: `origin/main` at `ed88d3255bbb8a40e5b7d00d01e5b10c73461660`
- Root Kanban task: `t_7ec21f55`
- Investigator handoff: `t_f65c1857` (durable handoff comment `#468`)
- Current bounded Developer task: `t_917e9e15`
- Downstream Reviewer task: `t_f92869f9`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:138`
- Controller workspace binding: `/ws/projects/hermes-n8n-control-plane/.worktrees/t_917e9e15`
- Delivery branch: `fix/issue-138-ready-binding-preflight`
- Delivery PR: PR #149 — `Issue #138: 구현·재작업 카드 생성 전 작업공간 바인딩 검증` — `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/149`
- Merge/auto-merge authority: human/user only
- Automation stop state: `NONE`; no live deployment or config mutation was performed in this round.

## Objective and confirmed cause

At the exact pre-round PR #149 head `ede92845011ca09f8e42517fdc216b4cdb7e44e3` (post-sync pre-fix checkpoint `d30300f392d276d44f3398c5f7c7160e3b155a68`), the shared parser passed escaped legacy-backtick delimiters inside an active outer substitution to `consume_substitution` without recording or recursively inspecting the nested body. Real Bash executes the nested `create`, `assign`, and `reassign` forms, while the parser and stable guard returned allow/rc=0.

The bounded fix keeps one parser owner: an escaped legacy-backtick delimiter is recorded and recursively inspected only when the scanner is already inside an executable substitution or arithmetic body. The existing quote/escape, malformed, and depth handling remains in force; top-level escaped data, single-quoted data, and harmless substitutions remain inert; and no shell predicate or broad interpreter is evaluated.

## Scope and non-goals

In scope for this round:

1. `automation/hermes/scripts/kanban-specialist-completion-guard.py`: recursively record escaped legacy-backtick bodies only from an already-active executable substitution/arithmetic scanner.
2. Parser regressions for nested legacy-backtick `create`, `assign`, and `reassign`, including arithmetic-wrapped forms, while preserving top-level escaped-data and harmless/documentation controls.
3. Stable `kanban-block-kind-guard.py` and workspace-binding no-mutation regressions proving rc=2, bounded diagnostics, byte-identical board state, zero fake-shell invocation, and no task/assignment/reassignment mutation.
4. This tracked REQ provenance refresh after implementation publication.

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

- Required causal RED: on the post-sync pre-fix checkpoint `d30300f392d276d44f3398c5f7c7160e3b155a68` with the current regression files, the nested parser/stable-hook/workspace matrix was `18 failed, 267 deselected`; the parser cases did not raise and the stable hook returned `0`. A separate Bash probe returned `0` for all nested `create`, `assign`, and `reassign` forms and recorded all three fake-Hermes calls.
- Focused current-round matrix after the fix: `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q tests/test_specialist_completion_contract_parser.py tests/test_specialist_completion_contract_guard.py tests/test_kanban_workspace_binding_guard.py` → `285 passed`; the nested regression filter alone → `18 passed, 267 deselected`.
- Focused stable-hook/workspace cases assert rc=2, bounded diagnostics, no shell invocation, unchanged DB bytes, and no task/assignment activity for all three action families and arithmetic-wrapped forms.
- Real-core parser/handler probes from the preserved Issue #138 contract: atomic `barrier`, `rollback`, `paths`, `same-key`, `lifecycle`, and `duplicates` scenarios PASS; handler `padded`, `cli-surface`, and `shell` scenarios PASS.
- Direct edge scripts after the latest main sync: workspace admission `27 passed, 0 failed` (one optional actual-core/entrypoint regression skipped because `hermes_cli` is not installed); workspace self-heal `38 passed`; race gate `5/5` in each of two iterations; completion, dependency, and head-binding gates PASS; terminal convergence `100 passed`.
- Rework/projection suites: delivery provenance `9 passed`; attention delivery recovery `8 passed`; edge projection/label history `4 passed`; parking-comment `20 passed`; GitHub-sync rework and related sync scripts exited `0`.
- Block-kind suite on candidate: `63 passed, 2 failed`; the two failures are legacy deployer dry-run fixtures that require the newer H4V3 profile-config layout. Fresh `origin/main` baseline is `65 passed`, so these are retained pre-existing PR #149 fixture failures; no block-kind production file changed in this round.
- Auxiliary attention self-heal-label script is `FAIL` for the pre-existing `agent_review_ready_predicted` fixture mismatch; the same assertion fails on fresh `origin/main`, and the script does not import or modify the changed parser path.
- Full repository candidate vs fresh `origin/main`: candidate `656 passed, 4 failed, 23 errors`; baseline `394 passed, 4 failed, 23 errors`. The same four completion-wake failures and 23 board-identity fixture setup errors occurred on both trees; no candidate-only failure/error identity was observed.
- N8N validator: `python3 automation/n8n/scripts/validate.py` → `{"ok": true, "schedule_workflows": 0, "edge_sync_workflows": 1, "github_workflows": 1, "github_event_router": 1, "edge_sync_execution": "n8n-webhook->direct-actuator:5682", "hermes_cron_required": false, "hermes_schedule_owned_by_n8n": false}`.
- Static checks: changed-file `python3 -m py_compile` PASS; Ruff `E4,E7,E9,F` PASS (`All checks passed!`); basedpyright `--level error` PASS (`0 errors, 0 warnings, 0 notes`); profile `/home/hermes/.hermes/profiles/kanban-main/lsp/node_modules/.bin/pyright --level error` PASS (`0 errors, 0 warnings, 0 informations`); cumulative changed shell `bash -n` and ShellCheck PASS; `git diff --check` PASS.
- GitHub Actions are disabled by repository policy and were not used as a substitute for local validation.

## Delivery and rollback boundary

- Implementation checkpoint: `27f9024` (`fix(kanban): close nested legacy-backtick mutation paths`); the main-sync publication head before this REQ refresh was `4454e6fa734c1568c1c37baec9d75730e43db14d`. The subsequent REQ-only provenance commit is authoritative only after remote read-back.
- Stable live entrypoint identity remains the repository-approved `automation/hermes/scripts/kanban-block-kind-guard.py`; it delegates terminal classification to the shared specialist parser and then to the existing workspace-binding guard for native creation. This round did not deploy or mutate that live hook/config.
- Rollback boundary is the existing PR #149 branch: revert the substitution-parser commit and this REQ-only provenance commit as ordinary branch commits; no Hermes core rollback or live host operation is required for this round.
- PR #149 was fresh-read after implementation publication as OPEN/unmerged, base `main`, branch `fix/issue-138-ready-binding-preflight`, with exactly one Issue #138-linked PR and visible plain-text `Closes #138.`; the REST/GraphQL head was `4454e6fa734c1568c1c37baec9d75730e43db14d`. The exact final head after this REQ refresh is authoritative only from post-push GitHub/remote read-back and Kanban handoff, not from a self-referential SHA embedded in this file.
- Merge/auto-merge was not performed. Internal Developer completion remains provisional until the independent Reviewer and canonical edge reconciliation complete.

Closes #138.
