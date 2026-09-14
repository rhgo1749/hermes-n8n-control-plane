# REQ-154 — trajectory telemetry/evaluation MVP

## Exact provenance

- Source Issue: #154 — agent trajectory/token/rework telemetry
- Repository: `rhgo1749/hermes-n8n-control-plane`
- Issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/154
- Intake/root task: `t_e0acd722`
- Intake idempotency key: github:rhgo1749/hermes-n8n-control-plane:issue:154
- Kanban implementation task: `t_4359ef0c`
- Investigator handoff: `t_eed82c1b`
- Source base used: `4043ec1bb8db4383dce822ec77d377da44bfee9b`

## Product type and bounded scope

Product type: repository-owned read-only dashboard/report contract.

Implement schema `h4v3-trajectory-v1` and the H4V3 Overview routes for one
strictly anchored GitHub Issue and a period aggregate. Reconstruct specialist
rounds from existing Kanban `tasks`, `task_links`, `task_runs`, and `task_events`
plus profile-local `state.db` session/model usage. Preserve profile/run/event
provenance, wall-clock versus worker timing, explicit reviewer rework,
investigation refresh, infrastructure retry, operator intervention, first-pass
review, and fresh GitHub Issue/PR closure evidence. Keep missing telemetry
explicitly `unknown`/`unavailable`/`partial` rather than zero.

## Non-goals and preserved contracts

- No Hermes core changes, new telemetry/state/notification database, n8n
  analytics source of truth, lifecycle mutation, worker control, or change to
  `agent-*` semantics.
- Never use prompt/body/title text, transcript length, self-reported usage,
  secrets, auth headers, or raw provider responses as telemetry.
- Never treat internal `done` as GitHub completion; PR head, merge commit, and
  Issue closure remain separate.
- Do not modify historical Issue #138 / PR #149 evidence. Do not weaken existing
  tests. Host/profile activation remains an explicit installer gate.

## Canonical implementation and validation

- Code: `hermes-plugin/h4v3-overview/dashboard/trajectory_report.py` and
  `dashboard/plugin_api.py`.
- Deploy contract: `automation/hermes/scripts/install-h4v3-overview.sh`.
- Canonical docs: `docs/H4V3_OVERVIEW.md` plus the runtime-namespace rules in
  `AGENTS.md` / `docs/OPERATIONS.md`.
- Validation profiles: `pytest -q tests/test_trajectory_report.py tests/test_h4v3_overview.py`; `python3 -m py_compile` for both backend modules; `git diff --check`; live read-only #138 fixture smoke with fresh GitHub evidence supplied separately.
- Golden anchors: observed PR head
  `f23000b9771772b6210593d5e611b782e88ba351`; merge commit
  `4043ec1bb8db4383dce822ec77d377da44bfee9b`.

## Runtime namespace gate

Before classifying live deployment as blocked, run the repository-owned installer
preflight/dry-run in the worker's current namespace and record its
`H4V3_RUNTIME_CONTEXT ...` line.

- The current production Hermes runtime is `hermes-cloudcli-agent` with
  `/home/hermes/.hermes`.
- If the worker is already inside that runtime namespace and the runtime home is
  directly accessible, run the installer directly against
  `/home/hermes/.hermes`; do not attempt nested `docker exec`.
- A missing `docker` executable or host Docker socket is not by itself a
  capability blocker when the target runtime is already directly accessible.
- Host-level Docker is only the bridge used from the Ubuntu host when the caller
  is outside the target runtime namespace.
- Report a capability blocker only if direct runtime access and the authorized
  canonical bridge are both unavailable, with the exact preflight evidence.

## Automation stop state

Automation stop state: HUMAN_VALIDATION_REQUIRED (PR review/merge remains human authority).

Automation may create/update the delivery PR and report local validation. It
must not merge or enable auto-merge. The final PR body must contain the exact
plain-text line `Closes #154.` outside fences/backticks, and the live closing
Issue relationship must be verified before handoff.

## Operator acceptance

Run the installer in the active Hermes runtime namespace, restart the existing
dashboard supervisor, verify the read-only Overview and trajectory routes, then
repeat the same install/read-back path to prove idempotent convergence. When the
worker is already inside `hermes-cloudcli-agent`, this is a direct runtime
operation rather than a nested Docker operation. A runtime failure after that
namespace preflight is an explicit gate; it is not hidden behind a passing
repository test.
