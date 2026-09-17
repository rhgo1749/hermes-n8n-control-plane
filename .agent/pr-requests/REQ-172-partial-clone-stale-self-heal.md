# REQ-172: partial-clone stale checkout 안전 self-heal

- Status: Implemented; PR pending
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION` / `DOCS_ONLY`
- Validation profiles: `STATIC_UNIT` / `N8N_VALIDATE` / `HOST_DASHBOARD`
- Integration target branch: `main`
- Required work branch: `hermes-n8n-control-plane/t_c467ad39-issue-172-delivery-partial-clone-stale-s`
- Source-of-truth base: fetched `origin/main` at `a60f0a43a724fd45a470847d588b27dc63063a57`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-172-partial-clone-stale-self-heal.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#172`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/172
- Kanban task ID: `t_d45659e4` (root: `t_f6b56972`, selector: `t_0aed1776`, selected Investigator: `t_564fc1f4`, reviewer: `t_5f7ca227`)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:172`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

Allow a clean stale canonical `blob:none` partial clone to recover through the existing locked exact-SHA fetch, ancestry proof, and authenticated `git merge --ff-only --no-verify` path without trusting executable or ambiguous repository configuration.

## Confirmed boundary

- The existing `_onboarding_local_materialization_is_safe` gate rejected Git-generated `remote.origin.promisor=true` and `remote.origin.partialclonefilter=blob:none` before self-heal.
- The approved decision is a minimal coupled allowlist: both keys must occur exactly once; `promisor` is case-insensitively `true`; `partialclonefilter` is exactly `blob:none`. Missing or malformed pairs fail closed.
- All other `remote.*`, URL rewrite, credential/include/protocol/transport, filter/merge/diff executable, and local attribute sources remain fail-closed.
- Inspection, ancestry proof, and exact-SHA fetch use `GIT_NO_LAZY_FETCH=1`. Only the final authenticated ff-only merge may override it to allow reviewed promisor hydration.
- GitHub/Kanban/edge/n8n ownership, per-repository locking, exact metadata SHA, ancestry-before-mutation, and human merge authority remain unchanged.

## In scope

1. Extend the existing safety gate with the exact coupled partial-clone metadata allowlist.
2. Force lazy-fetch containment for onboarding Git commands and route the final merge through `_git_onboarding_with_environment(..., askpass=askpass, token=token)` with lazy fetch explicitly enabled only there.
3. Add real local partial-clone stale fixtures, authenticated-hydration assertions, unsupported/orphaned/duplicate negative cases, malicious remote-config fail-closed coverage, and full-vs-partial convergence coverage.
4. Synchronize the durable checkout contract in `docs/REPOSITORY_REGISTRY.md`.
5. Deploy only through `automation/hermes/scripts/deploy-intake-edge.sh`, independently read back source/target hashes, and run the fresh-SHA scoped `rhgo1749/H4V3-DJ#166` canary.

## Explicit non-goals

- Dirty, diverged, wrong-origin, or unsafe checkout auto-repair.
- `git reset --hard`, force updates, broad cleanup, overwrite, or a second provisioning path.
- Trusting all partial-clone settings, changing n8n lifecycle ownership, merge/auto-merge, or Hermes core changes.
- Enabling GitHub Actions or adding a hosted workflow.

## Required validation

- RED before the fix: current-bad partial-clone hydration and invalid metadata cases must fail.
- GREEN: `uv run --with pytest --with pyyaml python -m pytest -q tests/test_checkout_self_heal.py tests/test_full_fallback_onboarding_entrypoint.py tests/test_repo_scoped_intake.py`
- `python3 -m py_compile automation/hermes/scripts/github-agent-ready-kanban-intake.py automation/hermes/scripts/github-agent-ready-kanban-intake-entrypoint.py`
- `uv run --with pyyaml python automation/n8n/scripts/validate.py`
- `bash -n automation/hermes/scripts/deploy-intake-edge.sh`
- `git diff --check`
- Fresh deploy dry-run/apply and independent source/target SHA-256 read-back with no `.deploy-candidate-*` residue.
- Fresh GitHub default-branch SHA, scoped `H4V3-DJ#166` intake, canonical checkout/ref/cleanliness read-back, and `h4v3-dj` board idempotency-task read-back.

## Stop state

Implementation stops after deterministic local evidence, repository-owned deployment/read-back, one Korean PR publication, REST/GraphQL closing-link verification, and the scoped canary. Human review and merge/auto-merge remain external authority; no merge is performed.
