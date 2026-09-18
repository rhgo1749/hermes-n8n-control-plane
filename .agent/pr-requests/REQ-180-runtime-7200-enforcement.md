# REQ-180: H4V3 표준 runtime 7200 control-plane enforcement

- Status: In Progress
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`, live runtime deploy/read-back
- Integration target branch: `main`
- Required work branch: `hotfix/issue-180-runtime-7200-enforcement`
- Source-of-truth base: `origin/main@81e16a6af42458d3ae927fdc450a8f024b5a606e`
- Source issue: `rhgo1749/hermes-n8n-control-plane#180`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/180
- Kanban task ID: none (operator-authorized direct hotfix)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:180`
- Merge authority: user explicitly authorized this hotfix
- Automation stop state: `NONE`

## Objective

Remove model choice from the standard H4V3 execution wall-time while leaving
Hermes core untouched.

- Investigator candidates and bounded Main selectors: exactly 7200 seconds.
- Developer/Reviewer/Designer: exactly 7200 seconds by default.
- Eligible implementation task with explicit `runtime_class=large` in its body:
  exactly 10800 seconds.
- Structured `kanban_create`: repository-owned pre-tool wrapper canonicalizes
  the numeric runtime before durable materialization and reads it back.
- Terminal create bypass: fail closed unless the exact canonical runtime is
  supplied.
- Investigation marker `budget.max_runtime_seconds` is canonicalized and
  validated at exactly 7200.

## Confirmed defect

The prior 7200 hotfix changed model-facing contracts and the search upper bound,
but still accepted any positive search runtime up to 7200. Fresh live evidence
showed active cards with `max_runtime_seconds=1800` and watchdog failures such
as `elapsed 1814s > limit 1800s`.

Hermes core intentionally stores `max_runtime_seconds` per task with no global
runtime default. Core modification is explicitly out of scope.

## In scope

1. Structured runtime canonicalization in the existing approved pre-tool wrapper.
2. Durable read-back of the canonical runtime alongside retry read-back.
3. Exact runtime validation in the specialist/search guard.
4. Terminal bypass exact-runtime enforcement.
5. Canonical role-contract update and focused regression coverage.
6. Live deployment through `deploy-intake-edge.sh`.
7. Operator repair of currently active sub-7200 H4V3 cards after a fresh board scan.

## Non-goals

- No Hermes core changes.
- No n8n lifecycle ownership changes.
- No retry-budget semantic changes.
- No generic global timeout default outside H4V3 execution cards.

## Validation

- `python3 -m py_compile` on changed Python files.
- `python3 -m pytest -q tests/test_kanban_retry_compat_hotfix.py tests/test_specialist_completion_contract_guard.py`
  in the Hermes runtime environment where the launcher is available.
- `python3 tests/test_kanban_investigator_role.py` or equivalent focused role contract test.
- `git diff --check`.
- deployer dry-run → apply → deployed source/read-back.
- fresh DB read-back proving active repaired cards carry 7200.
