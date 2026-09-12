# REQ-138: invalid ready implementation/REWORK card preflight

- Status: Implementation complete / review pending
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `HERMES_PLUGIN`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `N8N_VALIDATE`, `HERMES_PLUGIN`
- Integration target branch: `main`; required worker branch: `wt/t_0c8e19ed`
- Source-of-truth base: `origin/main` at `a0432f46d94c2baa0d39a011c9d924ff5fd76e66`
- Remote delivery: update existing PR #149 only; merge authority is human/user only
- Pull request title/body/final report language: Korean
- Source Issue: `rhgo1749/hermes-n8n-control-plane#138`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/138`
- Root Kanban task: `t_7ec21f55`
- Prior Investigator handoff: `t_f5de81a4`
- Prior implementation task: `t_586a1100`
- Prior Reviewer task: `t_d9b220cb`
- Bounded rework task: `t_0c8e19ed`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:138`
- Stable task identity: `github:rhgo1749/hermes-n8n-control-plane:issue:138:rework-round-1:developer-followup-2`
- Controller workspace binding: `/ws/projects/hermes-n8n-control-plane/.worktrees/issue-138-rework-round-5`; requested branch `wt/issue-138-rework-round-5`
- Delivery branch: `fix/issue-138-ready-binding-preflight`
- Delivery PR: PR #149 — `Issue #138: 구현·재작업 카드 생성 전 작업공간 바인딩 검증` — `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/149`
- Automation stop state: `NONE` — repository-local gates complete; no live deployment/config mutation, GitHub lifecycle mutation, merge/auto-merge, new PR, or build artifact

## 1. Objective and confirmed cause

At the existing stable `kanban-block-kind-guard.py` pre-tool boundary, reject a specialist `hermes kanban create` before shell execution whenever a later relevant invocation is separated by an unsupported/background/pipeline operator. Preserve deterministic `true && create`, `false || create`, `false && create`, and `true || create` behavior, wrapper/completion classification, exact argv handling, unresolved-substitution rejection, and the existing single-parser/stable-hook ownership.

The shared parser in `automation/hermes/scripts/kanban-specialist-completion-guard.py` previously skipped a statically unreachable segment and could then continue across `&`, `|`, `;&`, `;;&`, or `|||`; the shell could still execute the later create while the guard returned allow. The fix defines the supported chain operators (`;`, `&&`, `||`) and raises `RuntimeError` before collecting any relevant invocation when an unsupported operator has a later relevant Hermes segment. The stable wrapper remains in-process and no durable task mutation occurs on rejection.

## 2. In scope and non-goals

In scope:

1. The fail-closed shared parser precheck in `automation/hermes/scripts/kanban-specialist-completion-guard.py`.
2. Parser, stable-hook, and workspace no-mutation regressions covering every listed unsupported operator plus literal short-circuit controls.
3. This shortened REQ provenance refresh for the current delivery.

The cumulative PR also preserves the prior atomic creation/read-back, canonical board resolver/lock, CAS, lifecycle replay, workspace admission/self-heal, stable wrapper/deployer, and GitHub-sync contracts delivered by earlier rounds; this rework does not redesign them.

Explicit non-goals: Hermes core/product edits, a second parser/dispatcher/store, arbitrary shell execution, live deployment or config-hook mutation, cron/polling, archived cleanup, force-rebind, new PR creation, GitHub merge/auto-merge, or enabling hosted Actions.

## 3. Changed files

Current round:

- `automation/hermes/scripts/kanban-specialist-completion-guard.py`
- `tests/test_specialist_completion_contract_parser.py`
- `tests/test_kanban_workspace_binding_guard.py`
- `.agent/pr-requests/REQ-138-reject-invalid-ready-implementation-cards.md`

Cumulative PR allowlist additionally includes the earlier-round workspace guard:

- `automation/hermes/scripts/kanban-workspace-binding-guard.py`

## 4. Validation evidence

- Focused parser/stable-hook/workspace/completion suite: PASS — `env -u HERMES_DELEGATED_CHILD_CONTEXT python3 -m pytest -q tests/test_specialist_completion_contract_parser.py tests/test_kanban_workspace_binding_guard.py tests/test_specialist_completion_contract_guard.py` → `106 passed`.
- Unsupported-operator coverage: PASS — parser and workspace no-mutation tests parameterize `&`, `|`, `;&`, `;;&`, and `|||`; stable-hook tests parameterize the same five; literal controls retain `false &&`, `true ||` as zero materializations and `true &&`, `false ||` as one.
- Sabotage RED: PASS — removing only the new parser precheck produced five parser failures and ten workspace no-mutation failures; the precheck was restored and the focused suite returned `106 passed`.
- Real core/race-sensitive probes: PASS — focused workspace tests repeat the create/claim barrier, read-back rollback, canonical resolver/lock matrix, and same-key duplicate convergence; prior lifecycle replay and duplicate-convergence probes remain green.
- Existing edge suites: PASS — workspace admission `45 passed`; workspace self-heal `38 passed`; block-kind fail-closed `62 passed`; rework delivery provenance `9 passed`; edge projection/label history `4 passed`; attention delivery recovery `8 passed`; terminal convergence `95 passed`; dependency and race gates PASS; parking-comment gate `20 passed`; GitHub-sync rework `925 passed`.
- Static checks: PASS — Ruff `E4,E7,E9,F`; basedpyright `0 errors, 0 warnings, 0 notes`; profile `lsp/bin/pyright-langserver` 1.1.412 diagnostics for four changed Python documents `0 errors/warnings`; `py_compile`; `bash -n`; and `git diff --check`.
- N8N validation: PASS — `python3 automation/n8n/scripts/validate.py` returned `{"ok": true, "schedule_workflows": 0, "edge_sync_workflows": 1, "github_workflows": 1, "github_event_router": 1}`.
- Isolated profile deployer: PASS — `python3 automation/hermes/scripts/deploy-kanban-investigator-profile-contracts.py --hermes-home /home/hermes/.hermes --dry-run`; all five profiles `changed=no`, `dry_run=true; no files changed`. The explicit canonical root is required because this worker's `HERMES_HOME` is profile-scoped.
- Full-suite candidate vs fresh `origin/main`: BASELINE-EQUIVALENT — candidate `470 passed, 4 failed, 23 errors`; fresh `origin/main` `387 passed, 4 failed, 23 errors`; the same four pre-existing completion-wake failures and 23 `tmp`-fixture setup errors occurred, with no candidate-only failure/error identity. Existing tests were not weakened or skipped.

## 5. Delivery provenance and stop state

- Last non-self-referential implementation/validation head: `dbac6fcf3952ca83843130b35f40b7754d34d34c` (full SHA; `fix(kanban): fail closed on unsupported shell operators`).
- This REQ refresh is intentionally not self-referenced: after the refresh commit is pushed, the exact final PR head is independently established by fresh REST, GraphQL, and remote-branch read-back and recorded in the Kanban completion handoff.
- PR #149 remains the existing Korean PR with visible plain-text `Closes #138.`; no second PR, merge, or auto-merge is performed. GitHub-hosted Actions remain disabled by repository policy.

Closes #138.
