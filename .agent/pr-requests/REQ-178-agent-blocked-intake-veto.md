# REQ-178: agent-blocked Issue 신규 intake veto hotfix

- Status: In Progress
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`, runtime deploy dry-run/apply/read-back
- Integration target branch: `main`
- Required work branch: `hotfix/issue-178-agent-blocked-intake`
- Source-of-truth base: `origin/main@b74af55b59293e741e75dc618e6374e3bbaaac8a`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Source issue: `rhgo1749/hermes-n8n-control-plane#178`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/178
- Kanban task ID: none (operator-authorized direct hotfix)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:178`
- Planning/implementation owner: operator-authorized ChatGPT session
- Automation stop state: `NONE`
- Merge authority: user explicitly authorized merge for this hotfix

## Objective

Prevent an open GitHub Issue carrying both `agent-ready` and `agent-blocked`
from being selected as a new intake candidate. Removing only
`agent-blocked` must restore eligibility while preserving the persistent
`agent-ready` authorization model.

## Confirmed background

- Canonical lifecycle defines `agent-ready` as persistent Issue-level work
  permission and `agent-blocked` as the current maintainer-attention hold.
- The intake importer currently queries `open + agent-ready` and does not
  veto `agent-blocked`.
- H4V3-DJ #166 demonstrated the defect: it was imported while both labels
  were present.
- PR `agent-rework + agent-working` overlap semantics are unrelated and
  explicitly out of scope.

Ownership impact: NONE. The existing GitHub/edge/Hermes ownership model is
preserved; this only closes an admission gap in the existing intake owner.

## In scope

1. Exclude `agent-blocked` Issues from live GitHub intake candidates.
2. Keep fixture/offline intake behavior aligned with live admission.
3. Add deterministic regression coverage.
4. Deploy the merged source through the repository-owned
   `deploy-intake-edge.sh` path and verify live source identity.

## Non-goals

- No Hermes core changes.
- No PR rework lifecycle changes.
- No n8n workflow/lifecycle ownership changes.
- Do not remove `agent-ready` when an Issue becomes blocked.
- No GitHub Actions enablement.

## Validation

Required local gates:

```bash
python3 tests/test_agent_blocked_intake.py
python3 tests/test_repo_scoped_intake.py
python3 -m py_compile automation/hermes/scripts/github-agent-ready-kanban-intake.py
git diff --check
```

Deployment gate:

```bash
automation/hermes/scripts/deploy-intake-edge.sh \
  --hermes-home /home/hermes/.hermes --dry-run
automation/hermes/scripts/deploy-intake-edge.sh \
  --hermes-home /home/hermes/.hermes
```

After apply, read back the deployed intake core and verify the
`agent-blocked` veto is present and matches the merged repository source.
