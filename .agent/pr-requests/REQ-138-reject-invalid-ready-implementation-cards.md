# REQ-138: invalid ready implementation/REWORK card preflight

- Status: Tracked contract published; post-publication evidence is maintained in PR #149 and the Kanban handoff; review pending
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `HERMES_PLUGIN`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `N8N_VALIDATE`, `HERMES_PLUGIN`
- Source Issue: `rhgo1749/hermes-n8n-control-plane#138`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/138`
- Source-of-truth base: `origin/main` at `ed88d3255bbb8a40e5b7d00d01e5b10c73461660`
- Root Kanban task: `t_7ec21f55`
- Investigator handoff: `t_f9052272`
- Current bounded Developer task: `t_3ca563f1`
- Downstream Reviewer task: `t_cabfd91c`
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:138`
- Controller workspace binding: `workspace_kind=worktree`, project `p_7f39082d`; the controller derives the isolated task-id workspace and branch
- Delivery branch: `fix/issue-138-ready-binding-preflight`
- Delivery PR: PR #149 — `Issue #138: 구현·재작업 카드 생성 전 작업공간 바인딩 검증` — `https://github.com/rhgo1749/hermes-n8n-control-plane/pull/149`
- Merge/auto-merge authority: human/user only
- Automation stop state: `HUMAN_VALIDATION_REQUIRED` for independent review and human merge authority; no merge or auto-merge is delegated.

## Objective and confirmed background

This request closes the two integration gaps identified by review `5191752525`
for the existing PR #149: the canonical operations runbook did not describe
the expanded lifecycle-guard deployment/recovery contract, and the final
candidate had not yet been deployed and independently read back through the
repository-owned deployer. It does not redesign the already-implemented
creation-time guard.

At the exact publication predecessor `c32763f08f5a9ac595730e40d6da392f3186e9a2`,
the bounded implementation already preserves one parser owner, fail-closed
specialist and workspace-binding checks, stable hook identity, and the valid
structured-create path. The Investigator found no new functional guard defect.
The earlier live state and backup inventory are read-only evidence only; they do
not prove that this final publication was deployed by the repository-owned
script.

## Ownership and security boundaries

- GitHub remains the external Issue/PR/review authority; Hermes Kanban remains
  the execution/dispatch authority; the edge remains the reconciliation owner.
- This round changes only the durable host-operations description and request
  evidence. It introduces no state store, dispatcher, lifecycle owner, cron
  path, n8n workflow, or Hermes-core change.
- The deployment acts only in the active Hermes runtime namespace. The current
  containerized target is `/home/hermes/.hermes` inside
  `hermes-cloudcli-agent`; the unrelated host `$HOME/.hermes` is not a target.
- No credentials, tokens, or secret-bearing config contents belong in this
  tracked request or in the PR evidence.

## Scope

1. Add a narrow `docs/OPERATIONS.md` subsection covering the active runtime
   namespace, host `docker exec` versus direct in-container invocation, the
   global plus five profile-local config prerequisites, dry-run/apply ordering,
   per-target atomic replacement, the approved three-entry fail-closed hook,
   legacy-reference retirement, candidate cleanup, hash/semantic read-back,
   same-run backups, exact rollback output, and the absent-target limitation.
2. Refresh this tracked REQ with current Issue/PR/Kanban provenance, the
   predecessor head, cumulative allowlist, preserved prior validation evidence,
   and an explicit boundary between tracked pre-publication state and
   post-publication live evidence.
3. Run the required repository-local documentation, fixture, parser/completion/
   workspace, edge, static, type, shell, n8n, and diff gates before publication.
4. Publish the two-file change to the existing PR, deploy only the exact final
   published head through `deploy-intake-edge.sh`, and record live read-back,
   backup/rollback, and one safe canary/archive result in PR/Kanban evidence.

## Explicit non-goals

Hermes core or product changes; a second parser, dispatcher, or state store;
arbitrary shell interpretation or predicate execution; GitHub lifecycle or
label changes; cron or polling; n8n changes; active-worker rebinding; new PR
creation; merge/auto-merge; hosted Actions; unrelated stale-test cleanup;
archaeological snapshot restoration; hand-copying runtime files; and any
deployment to the unrelated host `$HOME/.hermes`.

## Changed-file allowlist

Current round edits only:

- `docs/OPERATIONS.md`
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
- `docs/OPERATIONS.md`
- `tests/test_kanban_investigator_role.py`
- `tests/test_kanban_workspace_binding_guard.py`
- `tests/test_specialist_completion_contract_guard.py`
- `tests/test_specialist_completion_contract_parser.py`
- `edge/test-kanban-block-kind-failclosed.py`

No Hermes core, product repository, n8n workflow ownership, cron, or live
runtime file is tracked by this round. The authorized live deployment is an
external validation action against the exact published head, not a repository
file change.

## Validation evidence

- Preserved causal RED: on the post-sync pre-fix checkpoint
  `d30300f392d276d44f3398c5f7c7160e3b155a68`, the nested
  parser/stable-hook/workspace matrix was `18 failed, 267 deselected`; the
  parser cases did not raise and the stable hook returned allow/rc=0. A
  separate Bash probe returned `0` for all nested `create`, `assign`, and
  `reassign` forms and recorded all three fake-Hermes calls.
- Preserved focused implementation matrix after the fix:
  `env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 python3
  -m pytest -q tests/test_specialist_completion_contract_parser.py
  tests/test_specialist_completion_contract_guard.py
  tests/test_kanban_workspace_binding_guard.py` -> `285 passed`; the nested
  regression filter alone -> `18 passed, 267 deselected`.
- Preserved stable-hook/workspace evidence: rc=2, bounded diagnostics, no shell
  invocation, unchanged DB bytes, and no task/assignment activity for all
  three action families and arithmetic-wrapped forms.
- Preserved real-core parser/handler probes: atomic `barrier`, `rollback`,
  `paths`, `same-key`, `lifecycle`, and `duplicates` scenarios PASS; handler
  `padded`, `cli-surface`, and `shell` scenarios PASS.
- Preserved direct edge evidence after the latest main sync: workspace
  admission `27 passed, 0 failed` (one optional actual-core/entrypoint
  regression skipped because `hermes_cli` is not installed); workspace
  self-heal `38 passed`; race gate `5/5` in each of two iterations;
  completion, dependency, and head-binding gates PASS; terminal convergence
  `100 passed`.
- Preserved rework/projection evidence: delivery provenance `9 passed`,
  attention delivery recovery `8 passed`, edge projection/label history
  `4 passed`, parking-comment `20 passed`, and GitHub-sync rework plus related
  sync scripts exited `0`.
- Preserved block-kind fixture evidence: candidate `66 passed`; fresh detached
  `origin/main` `65 passed`. Both valid dry-run fixtures create
  `kanban-main`, `kanban-investigator`, `kanban-developer`, `kanban-reviewer`,
  and `kanban-designer` profile configs, while the production five-profile
  preflight remains unchanged; the explicit missing-profile case remains
  fail-closed.
- Preserved auxiliary attention self-heal-label result: FAIL for the
  pre-existing `agent_review_ready_predicted` fixture mismatch; the same
  assertion fails on fresh `origin/main`, and the script does not import or
  modify the changed parser path.
- Preserved full repository comparison: candidate `656 passed, 4 failed, 23
  errors`; baseline `394 passed, 4 failed, 23 errors`. The same four
  completion-wake failures and 23 board-identity fixture setup errors occurred
  on both trees; no candidate-only failure/error identity was observed.
- Preserved n8n validator result: `python3 automation/n8n/scripts/validate.py`
  -> `ok=true`, `schedule_workflows=0`, `edge_sync_workflows=1`,
  `github_workflows=1`, `github_event_router=1`,
  `edge_sync_execution=n8n-webhook->direct-actuator:5682`,
  `hermes_cron_required=false`, and `hermes_schedule_owned_by_n8n=false`.
- Preserved static results: changed-file `py_compile`, Ruff `E4,E7,E9,F`,
  basedpyright `--level error`, profile Pyright severity-1, cumulative changed
  shell `bash -n` and ShellCheck, and `git diff --check` all PASS.
- GitHub Actions are disabled by repository policy and are not a substitute for
  local validation.

### This publication's local gate record

The final values for this documentation/evidence round are recorded only
after the second post-edit inspection loop and are kept separate from the
live-host gate. No unrun command is represented as PASS here. The final PR and
Kanban handoff must identify each applicable command and distinguish PASS,
FAIL, and NOT RUN with its reason.

## Exact-head deployment, read-back, and canary handoff

This REQ is the tracked pre-publication contract. The exact publication
predecessor was `c32763f08f5a9ac595730e40d6da392f3186e9a2`. The SHA of the
commit that publishes this file is intentionally not embedded here: the exact
final PR head is authoritative only from fresh GitHub/remote read-back and the
post-publication PR/Kanban handoff, avoiding a self-referential SHA fixed point.

After that publication, the Developer must record the following live evidence
outside this immutable tracked snapshot:

- the exact PR #149 full head SHA, branch/base/state, sole Issue #138 closing
  reference, and the matching remote branch ref;
- the repository-owned `automation/hermes/scripts/deploy-intake-edge.sh`
  invocation from that exact checkout, first `--dry-run` and then apply, in the
  active `hermes-cloudcli-agent` namespace against `/home/hermes/.hermes`;
- source/target SHA-256 comparisons for every deployer-mapped target, including
  the lifecycle guard, its core, the specialist completion guard, and the
  workspace-binding guard;
- semantic read-back of the global config and all five profile-local configs:
  one shared `python3 /home/hermes/.hermes/scripts/kanban-block-kind-guard.py`
  command for `kanban_block`, `kanban_create`, and `terminal`, with timeout 10,
  `fail_closed=true`, and zero live `kanban-workspace-guard.py` references;
- a zero-result `.deploy-candidate-*` residue inspection, the timestamped
  same-run backup set, exact printed rollback commands, and the pre-state of
  any target that was absent before deployment;
- exactly one native structured canary with `workspace_kind=worktree`, project
  `p_7f39082d`, `completion_contract=local-only`, a final-head-bound unique
  idempotency key, and a real unresolved parent. It must remain `todo` behind
  that parent, never claim/spawn/run/materialize a worktree, then be archived
  through the canonical board operation with event history read back.

The canary must not use a CLI/ad-hoc worktree fallback, a parentless or
preblocked placeholder, or an unrelated board. If a verification gate fails,
rollback is allowed only with the exact same-run backup paths printed by the
deployer, followed by fresh source/config/residue read-back. A target absent
before deployment has no automatic same-run restore command; do not claim that
rollback recreated its absence. Do not restore archaeological snapshots or
change cron, n8n, Hermes core, or unrelated runtime state.

## Delivery and rollback boundary

- Stable live entrypoint identity remains the repository-approved
  `automation/hermes/scripts/kanban-block-kind-guard.py`; the tracked round
  does not alter its implementation or the workspace-binding policy.
- The only delivery is existing PR #149 on
  `fix/issue-138-ready-binding-preflight`; the exact final head, deployment
  result, read-back hashes, and canary/archive identity come from fresh
  post-publication GitHub, runtime, and Kanban evidence, not inferred backups.
- Merge/auto-merge was not performed and remains human/user authority. Internal
  Developer completion remains provisional until the independent Reviewer and
  canonical edge reconciliation complete.

Closes #138.
