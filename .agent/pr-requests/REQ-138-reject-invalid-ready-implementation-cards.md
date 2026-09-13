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
- Current bounded Developer task: `t_76867a9a`
- Downstream Reviewer task: `t_c63ba0ac`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:138`
- Controller workspace binding: `/ws/projects/hermes-n8n-control-plane/.worktrees/t_76867a9a`
- Delivery branch: `fix/issue-138-ready-binding-preflight`
- Delivery PR: PR #149 — `Issue #138: 구현·재작업 카드 생성 전 작업공간 바인딩 검증` — `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/149`
- Merge/auto-merge authority: human/user only
- Automation stop state: `NONE`; no live deployment or config mutation was performed in this round.

## Objective and confirmed cause

At the pre-round PR #149 head `c5939ae0e672c1a6fad61d6bbe281e9cc056dc3e`, the shared parser in `automation/hermes/scripts/kanban-specialist-completion-guard.py` tokenized `;&|` but not shell grouping `(`, `)`, `{`, or `}`. `_first_executable()` therefore saw `(` or `{` instead of the reachable `hermes` command, so grouped `create`, `assign`, and `reassign` mutations were irrelevant to the stable hook and could reach the real shell with rc=0.

The bounded fix keeps one parser owner and rejects an unquoted grouping body that contains a relevant specialist Kanban mutation before the supported command-chain parser can allow it. The grouping scanner is lexical and bounded: it ignores quoted/escaped data, descends only to detect recognized direct or supported-shell-wrapper `hermes kanban create|assign|reassign` forms, never executes predicates, and never flattens grouping into the supported grammar. Existing unsupported/background/pipeline operator rejection and literal deterministic `false &&` / `true ||` / `true &&` / `false ||` behavior remain unchanged.

## Scope and non-goals

In scope for this round:

1. `automation/hermes/scripts/kanban-specialist-completion-guard.py`: fail-closed detection of relevant `(...)` and `{...;}` grouping.
2. Parser regressions for grouping and `&`, `|`, `;&`, `;;&`, `|||` across `create`, `assign`, and `reassign`.
3. Stable `kanban-block-kind-guard.py` wrapper and workspace-binding no-mutation regressions proving rc=2, diagnostics, byte-identical board state, zero rows/adapter activity, and no assignment/reassignment.
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

- Expected RED against a clean c5939ae parser copy: `3 failed` grouped-mutation assertions (each baseline command did not raise the required grouping error); this proves the new regression bites.
- Focused current-round suite: `env -u HERMES_DELEGATED_CHILD_CONTEXT python3 -m pytest -q tests/test_specialist_completion_contract_parser.py tests/test_specialist_completion_contract_guard.py tests/test_kanban_workspace_binding_guard.py` → `174 passed`.
- Race-sensitive repeat: workspace binding + edge single-flight + GitHub-sync race/convergence selection → `131 passed` in iteration 1 and `131 passed` in iteration 2.
- Direct edge scripts: workspace admission `27 passed`; workspace self-heal `38 passed`; race gate `5/5`; dependency gate `10/10`; completion gate `22/22`.
- Rework/projection suites: delivery provenance `9 passed`; attention delivery recovery `8 passed`; edge projection/label history `4 passed`; terminal convergence `19 passed`; GitHub-sync rework `166 passed`; parking-comment `6 passed`.
- Block-kind suite on candidate: `60 passed, 2 failed` in legacy deployer fixtures that omit the now-required H4V3 profile configs. Fresh `origin/main` baseline: `62 passed`; the two candidate failures are from the cumulative prior deployer contract and outside this round's diff.
- Attention-label self-heal suite: candidate `2 failed, 2 passed`; fresh `origin/main` has the same `2 failed, 2 passed` identities (`agent_review_ready_predicted` / retry-call expectations), so no candidate-only failure.
- Full repository candidate vs fresh `origin/main`: candidate `545 passed, 4 failed, 23 errors`; baseline `394 passed, 4 failed, 23 errors`. The same four completion-wake failures and 23 board-identity fixture setup errors occurred on both trees; no candidate-only failure/error identity was observed.
- N8N validator: `python3 automation/n8n/scripts/validate.py` → `{"ok": true, "schedule_workflows": 0, "edge_sync_workflows": 1, "github_workflows": 1, "github_event_router": 1, "edge_sync_execution": "n8n-webhook->direct-actuator:5682", "hermes_cron_required": false, "hermes_schedule_owned_by_n8n": false}`.
- Static checks: changed-file `python3 -m py_compile` PASS; Ruff `E4,E7,E9,F` PASS (`All checks passed!`); basedpyright `--level error` PASS (`0 errors, 0 warnings, 0 notes`); profile `lsp/bin/pyright-langserver` severity-1 diagnostics PASS (`0` diagnostics for all four changed Python files); `bash -n automation/hermes/scripts/deploy-intake-edge.sh` PASS; `git diff --check` PASS.
- Isolated deployer coverage: profile-aware `--dry-run` regression PASS with all six candidate configs byte-identical and no `.deploy-candidate-*` residue; isolated apply regression PASS with backups and no residue. No live config was touched.
- GitHub Actions are disabled by repository policy and were not used as a substitute for local validation.

## Delivery and rollback boundary

- Implementation checkpoint freshly validated and pushed before this REQ-only refresh: `e2fbb52acb1875564b1956e016226f79a70b4dc3` (`fix(kanban): reject grouped specialist shell mutations`).
- Stable live entrypoint identity remains the repository-approved `automation/hermes/scripts/kanban-block-kind-guard.py`; it delegates terminal classification to the shared specialist parser and then to the existing workspace-binding guard for native creation. This round did not deploy or mutate that live hook/config.
- Rollback boundary is the existing PR #149 branch: revert the grouping-parser commit and this REQ-only provenance commit as ordinary branch commits; no Hermes core rollback or live host operation is required for this round.
- PR #149 was fresh-read after the implementation push as OPEN/unmerged, base `main`, branch `fix/issue-138-ready-binding-preflight`, with exactly one Issue #138-linked PR and visible plain-text `Closes #138.`. The exact final head after the subsequent REQ publication is authoritative only from the post-push GitHub/remote read-back and Kanban handoff, not from a self-referential SHA embedded in this file.
- Merge/auto-merge was not performed. Internal Developer completion remains provisional until the independent Reviewer and canonical edge reconciliation complete.

Closes #138.
