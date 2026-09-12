# REQ-138: invalid ready implementation/REWORK card preflight

- Status: Implementation complete / review pending
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `HERMES_PLUGIN`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `N8N_VALIDATE`, `HERMES_PLUGIN`
- Integration target branch: `main`; required worker branch: `wt/t_b7258023`
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
- Earlier follow-up Reviewer task: `t_17883aa1`
- Prior-round Developer task: `t_2478d4a5`
- Prior provenance Developer task: `t_a53b154f`
- Prior provenance Reviewer task: `t_10072cbb`
- Prior rework Reviewer task: `t_000bf302`
- Prior Developer task: `t_b7258023`
- Prior follow-up Reviewer task: `t_5f4c60b5`
- Prior provenance-correction Developer task: `t_34bee817`
- Prior current-head Reviewer task: `t_9cc04b9c`
- Current provenance-correction Developer task: `t_8e8df685`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:138`
- Prior stable task identity: `github:rhgo1749/hermes-n8n-control-plane:issue:138:rework-round-8:developer`
- Controller workspace binding: `/ws/projects/hermes-n8n-control-plane/.worktrees/issue-138-rework-round-8`; requested branch `wt/t_b7258023`
- Delivery branch: `fix/issue-138-ready-binding-preflight`
- Delivery PR: PR #149 — `Issue #138: 구현·재작업 카드 생성 전 작업공간 바인딩 검증` — `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/149`
- Automation stop state: `NONE` after requested host/runtime deployment and canary verification; merge/auto-merge remains human/user authority.

## 0. Current hotfix addendum — CtrlHangul #113 live binding failure

On 2026-09-13, CtrlHangul Issue #113 intake exposed a deployment gap not covered by the earlier PR-only validation. The live Hermes config still registered the superseded `/home/hermes/.hermes/scripts/kanban-workspace-guard.py` for structured `kanban_create`. That 2026-08-26 guard reads legacy/non-schema keys `workspace` and `branch`, while the live Hermes structured schema exposes `workspace_kind`, `workspace_path`, and `project`; structured specialist creates were therefore misclassified as scratch/no-workspace before the repository-owned workspace-binding preflight could run. Main then explored CLI/manual worktree fallback, which is explicitly the wrong recovery boundary.

This hotfix keeps PR #149 and the existing `fix/issue-138-ready-binding-preflight` branch. It retires only the superseded hook entries during repository-owned config rendering, leaves the old runtime file untouched for rollback archaeology, verifies the candidate config contains exactly the stable approved lifecycle wrapper and no legacy workspace hook, and teaches the Main profile/canonical role contract to use structured `kanban_create` with `workspace_kind="worktree"` + canonical `project` + stable `idempotency_key` while omitting `workspace_path`/branch fields. The current host base was freshly fetched at `origin/main=c585d168cf5f6d3be8b714520b09d9cbb3e720b8`; the pre-hotfix PR #149 head observed from GitHub was `9858882e2f7017096e34b2608705b499857ec850`.

## 1. Objective and confirmed cause

At the existing stable `kanban-block-kind-guard.py` pre-tool boundary, reject relevant specialist `hermes kanban create`, `assign`, and `reassign` reachability before shell execution whenever an invocation depends on an unsupported/background/pipeline operator or ambiguous conditional. Preserve deterministic `true && create`, `false || create`, `false && create`, and `true || create` behavior, wrapper/completion classification, exact argv handling, unresolved-substitution rejection, and the existing single-parser/stable-hook ownership.

The shared parser in `automation/hermes/scripts/kanban-specialist-completion-guard.py` previously skipped a statically unreachable segment and could then continue across `&`, `|`, `;&`, `;;&`, or `|||`; the shell could still execute a later `reassign` while the guard returned allow because the shared relevance predicate only named `create` and `assign`. The bounded fix includes `reassign` in that predicate, so the existing supported-chain (`;`, `&&`, `||`) and fail-closed checks reject all five unsupported operators and `if true; then ... reassign ...; fi` before collecting any relevant invocation. The stable wrapper remains in-process and no durable task mutation occurs on rejection.

## 2. In scope and non-goals

In scope:

1. The fail-closed shared parser precheck in `automation/hermes/scripts/kanban-specialist-completion-guard.py`.
2. Parser, stable-hook, and workspace no-mutation regressions covering every listed unsupported operator plus literal short-circuit controls.
3. This shortened REQ provenance refresh for the current delivery.

The cumulative PR also preserves the prior atomic creation/read-back, canonical board resolver/lock, CAS, lifecycle replay, workspace admission/self-heal, stable wrapper/deployer, and GitHub-sync contracts delivered by earlier rounds; this rework does not redesign them.

Explicit non-goals: Hermes core/product edits, a second parser/dispatcher/store, arbitrary shell execution, live deployment or config-hook mutation, cron/polling, archived cleanup, force-rebind, new PR creation, GitHub merge/auto-merge, or enabling hosted Actions.

## 3. Changed files

Current hotfix round:

- `automation/hermes/scripts/kanban-block-kind-hook-config.py` — retire superseded live workspace-hook entries during config render.
- `automation/hermes/scripts/deploy-intake-edge.sh` — reject candidate configs that still reference the superseded hook and report the migration.
- `automation/hermes/profile-contracts/kanban-main-investigator.md` — structured specialist workspace creation contract; no CLI/manual worktree fallback.
- `docs/KANBAN_ROLE_CONTRACTS.md` — durable Main creation boundary.
- `tests/test_specialist_completion_contract_guard.py` — legacy-hook config migration/deployer regression.
- `tests/test_kanban_investigator_role.py` — deployed Main contract regression.
- `.agent/pr-requests/REQ-138-reject-invalid-ready-implementation-cards.md` — current runtime evidence and provenance.

Cumulative PR allowlist additionally includes earlier-round files:

- `automation/hermes/scripts/deploy-intake-edge.sh`
- `automation/hermes/scripts/kanban-block-kind-guard.py`
- `automation/hermes/scripts/kanban-workspace-binding-guard.py`
- `tests/test_specialist_completion_contract_guard.py`

## 4. Validation evidence

- Focused parser/stable-hook/workspace/completion suite: PASS — `env -u HERMES_DELEGATED_CHILD_CONTEXT python3 -m pytest -q tests/test_specialist_completion_contract_parser.py tests/test_kanban_workspace_binding_guard.py tests/test_specialist_completion_contract_guard.py` → `124 passed`.
- Unsupported-operator and conditional reassign coverage: PASS — parser, workspace no-mutation, and stable-hook regressions cover `&`, `|`, `;&`, `;;&`, `|||`, plus `if true; then ... hermes kanban reassign ...; fi`; literal `false && create`, `true || create`, `true && create`, and `false || create` behavior remains covered.
- Explicit stable-hook probes: PASS — six cases returned `2`, emitted a blocking diagnostic, and left the temporary board file byte-identical (`&`, `|`, `;&`, `;;&`, `|||`, conditional `reassign`).
- Sabotage RED: PASS — removing only `reassign` from the shared predicate produced `13 failed, 81 deselected` across the new parser/workspace/stable tests; restoring it returned the focused suite to `124 passed`.
- Real core/race-sensitive probes: PASS — focused workspace tests retain create/claim barrier, read-back rollback, canonical resolver/lock matrix, and same-key duplicate convergence; prior lifecycle replay and duplicate-convergence probes remain green.
- Existing edge suites: PASS — workspace admission `27 passed`; workspace self-heal `38 passed`; block-kind fail-closed `62 passed`; rework delivery provenance `9 passed`; rework attention recovery `8 passed`; edge projection/label history `4 passed`; terminal convergence `95 passed`; dependency and race gates PASS; parking-comment gate `20 passed`; GitHub-sync rework `925 passed`.
- Additional baseline check: `edge/test-kanban-rework-attention-selfheal-label.py` fails on candidate and fresh `origin/main` with the same `agent_review_ready_predicted` assertion; it is outside this bounded diff and is not claimed as passed.
- Static checks: PASS — `python3 -m py_compile` changed Python; Ruff `E4,E7,E9,F` (`All checks passed!`); basedpyright `0 errors, 0 warnings, 0 notes`; profile `lsp/bin/pyright-langserver` diagnostics for six changed Python documents `0 errors/warnings`; `bash -n automation/hermes/scripts/deploy-intake-edge.sh`; and `git diff --check`.
- N8N validation: PASS — `python3 automation/n8n/scripts/validate.py` returned `{"ok": true, "schedule_workflows": 0, "edge_sync_workflows": 1, "github_workflows": 1, "github_event_router": 1}`.
- Isolated profile deployer: PASS — `python3 automation/hermes/scripts/deploy-kanban-investigator-profile-contracts.py --hermes-home /home/hermes/.hermes --dry-run`; all five profiles `changed=no`, `dry_run=true; no files changed`.
- Full-suite candidate vs fresh `origin/main`: BASELINE-EQUIVALENT — candidate `488 passed, 4 failed, 23 errors`; fresh `origin/main` `387 passed, 4 failed, 23 errors`; the same four completion-wake failures and 23 `tmp`-fixture setup errors occurred, with no candidate-only failure/error identity. Existing tests were not weakened, skipped, or relaxed.

## 5. Delivery provenance and stop state

- Functional validation commit for this bounded rework: `d197db38751fdc783a8725e109fc9e996b86b869` (`fix(kanban): guard reassign shell reachability`); all focused/static/edge/full-baseline validation above was run after restoring this implementation.
- Prior validated implementation head for this rework: `f7db99c984c27294d36c2df1381a91f3ecaa51ec`; it is distinct from the functional validation commit above and remains explicitly prior validated implementation provenance, not the current or final PR head.
- Published provenance correction checkpoint: `d598a36f2d118044021c2109c2c35bf522427a13`, whose parent is the prior validated implementation head `f7db99c984c27294d36c2df1381a91f3ecaa51ec`; this correction changes only the tracked REQ file.
- Historical/intermediate round-8 publication checkpoint: `fb8d59568a1bf0531023b8f04f34b81c41e11d1b`; this is a provenance-only descendant and is not the current PR head.
- Last validated/delivered implementation head before the current provenance-only publication: `824e65471c7fd912b3ac6d3c0542935e715c5b0c`; this remains the exact last validated/delivered implementation identity, but is not the current remote branch/PR head after the current publication.
- Current published branch/PR head: `de0cb651a569e1a1935d041a82c962fd61a7200d`; this is the exact current full head observed for PR #149 and the delivery branch before this bounded provenance-only correction. The earlier `8b3162d3a2b6c5b5a5f354e809254d93ae2185d9` and `7481bc8459be708559edd1e26d5d1d1eae7d9a26` checkpoints are prior/intermediate provenance, not the current/final head.
- Fresh Reviewer `t_000bf302` returned REWORK solely because the previous provenance correction was local; prior Developer `t_b7258023` published the preceding correction, Reviewer `t_5f4c60b5` identified the earlier stale current-head identity, prior provenance-correction Developer `t_34bee817` published the last bounded REQ-only update, and Reviewer `t_9cc04b9c` identified that the actual current head was still omitted. Current provenance-correction Developer `t_8e8df685` published the preceding bounded REQ-only update; Fresh Reviewer `t_7597e045` identified `provenance-current-head-omitted-after-correction`; prior rework Developer `t_e31fdd41` published the preceding bounded REQ-only update; Fresh Reviewer `t_8b323d9f` identified the same bounded current-head provenance defect; current rework Developer `t_b537c4b1` publishes this bounded REQ-only update.
- Source/provenance chain: root `t_7ec21f55`; Investigator `t_f5de81a4`; prior Developer `t_586a1100`; prior Reviewer `t_d9b220cb`; bounded Developer `t_0c8e19ed`; earlier follow-up Reviewer `t_17883aa1`; prior-round Developer `t_2478d4a5`; prior provenance Developer `t_a53b154f`; prior provenance Reviewer `t_10072cbb`; prior rework Reviewer `t_000bf302`; prior Developer `t_b7258023`; prior follow-up Reviewer `t_5f4c60b5`; prior provenance-correction Developer `t_34bee817`; prior current-head Reviewer `t_9cc04b9c`; current provenance-correction Developer `t_8e8df685`; prior rework Developer `t_e31fdd41`; current rework Reviewer `t_8b323d9f`; current rework Developer `t_b537c4b1`.
- PR #149 remains the existing Korean PR on `fix/issue-138-ready-binding-preflight`; its current published branch/PR head is `de0cb651a569e1a1935d041a82c962fd61a7200d`, while `824e65471c7fd912b3ac6d3c0542935e715c5b0c` remains the last validated/delivered implementation head. The earlier `8b3162d3a2b6c5b5a5f354e809254d93ae2185d9` and `7481bc8459be708559edd1e26d5d1d1eae7d9a26` checkpoints are prior/intermediate, not current/final. Its base is `main`, and its REST/GraphQL state is OPEN/unmerged with `closingIssuesReferences=[138]`. Its visible plain-text closing marker is `Closes #138.`. No second PR, merge, auto-merge, live deployment/config mutation, or hosted Actions change is performed.

Closes #138.
