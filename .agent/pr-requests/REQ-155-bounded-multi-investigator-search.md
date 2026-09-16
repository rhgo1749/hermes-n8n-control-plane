# REQ-155: 고불확실성 bounded Investigator search 경로

- Status: Implemented; PR #171 open; human validation required
- Project: `hermes-n8n-control-plane`
- Product type: `HERMES_PLUGIN` / `DOCS_ONLY`
- Validation profiles: `STATIC_UNIT` / `HERMES_PLUGIN` / `HOST_DASHBOARD`
- Integration target branch: `main`
- Required work branch: `feat/issue-155-bounded-investigator-search`
- Source-of-truth base: `origin/main` at `1f1d3ad91512a9fff98fc7e1b4c59d98e362c8e4`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-155-bounded-multi-investigator-search.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#155`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/155
- Kanban task ID: `t_5b9947dd` (root: `t_41397dad`, Investigator: `t_54669e81`; bounded-admission rework: `t_e55b55bb`; round-2 rework: `t_79b8a5dd`)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:155`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

기계적 작업은 기존 N=1 Investigator 경로를 유지하고, 명시적 고불확실성 trigger에서만 최대 두 개의 독립 Investigator candidate를 fan-out한 뒤 Main selector가 closure/evidence/validation cost를 비교한다. Developer는 selector가 승인한 handoff만 primary context로 받고 Reviewer REWORK는 구현 누락과 model-refresh 필요를 구분한다.

## Confirmed boundary

- Main/Investigator/Developer/Reviewer ownership, `completion_contract=local-only`, deterministic edge/controller, `agent-*` 의미, human merge authority, merged PR #167 terminal-specialist→intake-root join을 보존한다.
- Candidate/selector metadata는 기존 Kanban task/run/event evidence에 `investigation_search` (`h4v3-investigation-search-v1`) marker로 남기고 별도 DB/store를 만들지 않는다.
- H4V3 Overview trajectory report는 read-only projection이다. Existing `h4v3-trajectory-v1` usage/role/rework/refresh/infra fields는 유지하며 search usage/candidate/selection/cost/outcome를 additive fields로만 투영한다.

## In scope

1. `kanban-main` contract에 N=1 default, deterministic trigger, exact N=2/one-expansion graph, selector, selected-only context, fan-out/runtime/retry admission rules를 추가한다.
2. `kanban-investigator` contract에 candidate identity/independence와 `observed_failure`, `root_cause_model`, `generalized_invariant`, `equivalence_classes`, `falsification_plan`, `completion_oracle`, `residual_unknowns` closure를 추가한다.
3. `kanban-reviewer` contract와 canonical role document에 local implementation gap vs `investigation_model_refresh` classification을 명시한다.
4. trajectory report에 explicit search marker 기반 count/selection/cost projection과 aggregate를 추가한다.
5. repository-owned deployer/dry-run/read-back contract tests를 갱신한다.

## Explicit non-goals

- Hermes core fork/change, second dispatcher, new profile, n8n workflow, telemetry DB, duplicate transcript store, ML selector policy, lifecycle-label authority, or merge/auto-merge.
- `candidate_count_after_initial`을 search evidence로 재해석하거나 missing usage를 zero로 추정하는 것.
- Prompt wording/post-run report를 실제 cumulative token/retry enforcement라고 주장하는 것.

## Budget gate

Fan-out is hard-capped at exactly two candidates and one expansion. The canonical lifecycle guard now records an atomic admission event in the existing root task's `task_events` ledger before each candidate mutation: each candidate reserves `ceil(max_total_tokens / 2)` tokens and one retry, with `max_total_tokens<=32000`, `max_retries=2`, and `max_runtime_seconds<=1800`. The existing dispatcher enforces the per-task runtime cap; prompt wording, `goal_max_turns`, and post-run telemetry are not substitutes. Admission is keyed by `search_id`, root task, candidate identity, and idempotency key; exhaustion fails closed before mutation. The trajectory report treats missing or malformed budget as unavailable/non-compliant rather than inferring compliance.

## Required validation

- Focused profile/deployer tests: N=1 default, trigger-gated N=2, independence, closure/LOW gating, selected-only handoff, no third candidate, and Reviewer rework classification.
- Focused trajectory tests: explicit no-search, marked A/B+selector, unmarked Investigator refresh, Reviewer PASS/REWORK, infra retry, and absent/partial usage; search fields remain distinct and unavailable values remain null/UNKNOWN.
- `uv run --with pytest --with fastapi python -m pytest -q tests/test_kanban_investigator_role.py tests/test_trajectory_report.py`
- `python3 -m py_compile` for changed Python; `bash -n` for changed shell; repository LSP/pyright and `git diff --check`.
- Disposable profile deploy dry-run → apply → identical rerun with no residue, then authorized live deploy/read-back if available. Live profile activation and dashboard restart remain separate host gates.
- Delivery PR must be Korean, contain visible plain-text `Closes #155.`, and be fresh-read through REST plus GraphQL `closingIssuesReferences`; do not merge.

## Stop state

Implementation worker stops after local evidence and PR publication/current-state read-back. Pending CI, human review, live dashboard/profile restart, operator budget policy, and merge are external/manual gates; none may keep a worker running.

## 2026-09-17 runtime guard follow-up

- Observed live failure: Hermes correctly scrubs `HERMES_KANBAN_TASK` from shell-hook child environments, so the H4V3 bounded-search guard could not prove the dispatcher-owned Main task and repeatedly failed closed while the worker kept reasoning.
- Repair boundary: H4V3 guard only; Hermes core remains unchanged. For native structured `kanban_create`, recover the direct dispatcher worker identity only when the hook carries a normal one-shot turn UUID and the canonical board proves one live task with the exact workspace, worker PID, and current run. Delegate-task identities (`sa-*`) do not recover the parent task.
- Validation: bounded-search guard `20 passed`; completion/parser/workspace guard suite `305 passed`; Investigator/trajectory suite `42 passed`; changed Python `py_compile`, deployer dry-run, live apply, source/live SHA-256 read-back, six hook-config semantic read-back, and no deploy-candidate residue all passed.
- Runtime configuration follow-up is intentionally outside Hermes core: all five H4V3 Kanban profiles use `approvals.single_query_mode=approve` and `terminal.timeout=180` so unattended `chat -q` workers do not dead-end on ordinary approval prompts or zero-second file-operation timeouts. Hardline approval floors and H4V3 fail-closed lifecycle guards remain active.

## 2026-09-17 slow-local runtime hotfix

- Bounded Investigator candidate/selector wall time is raised from 900s to 1800s for the local production model.
- The search admission budget remains two candidate retry reservations (`budget.max_retries=2`); this is distinct from the actual Kanban task breaker, which is now explicitly `max_retries=5`.
- Main creates Developer/Reviewer/Designer work with a 3600s cap by default and uses 5400s only when the task body explicitly records `runtime_class=large`.
- The repository-owned Kanban loop guard treats max-runtime timeouts and clean-exit protocol violations as the same five-attempt safety class; rate-limit exits remain excluded. Hermes core is unchanged.
