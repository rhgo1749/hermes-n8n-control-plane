# REQ-158: parked root dependency 복구가 active reviewer 진단에 가려지는 문제 수정

- Status: Review
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`, `N8N_VALIDATE`
- Integration target branch: `main`
- Required work branch: `hotfix/158-parked-root-dependency-recovery`
- Source-of-truth base: `origin/main` at `e8ee13718f8eb5b067f0d8743b1a73ecd43517f8`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-158-parked-root-dependency-recovery.md`
- Merge authority: Human/user only; user explicitly authorized hotfix merge and deployment in the originating operator session
- Source issue: `rhgo1749/hermes-n8n-control-plane#158`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/158`
- Kanban task ID: operator hotfix; no new execution card required
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:158`
- Planning/lead owner: operator
- Implementation owner: operator hotfix
- Automation stop state: `HOST_VALIDATION_REQUIRED` until deployed edge identity/read-back passes

## 0. Mandatory repository route

Read `AGENTS.md` -> `README.md` -> `docs/README.md` -> `docs/GITHUB_COMPLETION_LIFECYCLE.md` -> `edge/kanban-github-sync.py` and focused dependency/terminal-convergence tests.

## 1. Objective

Ensure a GitHub-backed intake root incorrectly parked in `review` is restored to `todo` when a live non-terminal internal parent exists, even when terminal-convergence detects active worker ownership. Preserve the active worker and all existing merge-authority/fail-closed behavior.

## 2. Confirmed background

- Runtime repro: Issue #138 root `t_7ec21f55` was `review`; reviewer `t_d6f73614` was `running`.
- After adding the missing canonical reviewer -> root parent link, deployed edge dry-run returned `terminal_convergence_active_ownership` and left the root in `review`.
- Canonical completion lifecycle requires pending direct parents to keep the root out of external projection and repairs stale `review`/`done` roots to `todo`.
- Root was operator-recovered via `reopen-review` and is currently dependency-gated.
- Ownership impact: NONE. Edge remains sole GitHub/Kanban reconciliation owner.
- New state/store introduced: NO.
- Existing authority bypassed: NO.
- Security/auth/network impact: NONE.

## 3. In scope

1. Fix pending-dependency ordering for `review` roots when terminal convergence reports active ownership.
2. Add deterministic regression covering `review root + active reviewer` and preservation of reviewer ownership.
3. Keep canonical lifecycle prose aligned if implementation detail needs clarification.
4. Deploy through repository-owned edge deployer and verify exact installed identity.

## 4. Explicit non-goals

- Hermes core changes.
- n8n lifecycle/state ownership changes.
- Changing merged stale-chain convergence semantics beyond the root-cause boundary.
- Broad refactor of terminal convergence.
- Unrelated cleanup.

## 5. Implementation requirements

- Dedicated worktree/branch only.
- Prefer the smallest ordering/fallback fix in canonical edge.
- Active reviewer claim/run/worker metadata must remain untouched.
- `todo` roots retain existing terminal-convergence diagnostics; the regression is specifically stale `review` repair.
- No external projection while a direct parent is non-terminal.

## 6. Validation contract

Required:

```bash
/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-dependency-gate.py
/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-terminal-convergence.py
env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py
python3 automation/n8n/scripts/validate.py
python3 -m py_compile edge/kanban-github-sync.py
git diff --check
```

Run Ruff E4/E7/E9/F and basedpyright on changed Python when available. `NOT RUN != PASS`.

## 7. Deployment / rollback

Deploy only after merge to `main` using `automation/hermes/scripts/deploy-intake-edge.sh --hermes-home /home/hermes/.hermes`. Verify deployed wrapper/core identity and a dry-run against the repaired Issue #138 root. Preserve the deployer-printed rollback path. Restart only if the deployer/contract requires it for this edge-only change.


## 8. Validation evidence

- RED proof before source fix: terminal-convergence suite `98 passed, 2 failed`; only the new parked-review regression failed because result stayed `terminal_convergence_active_ownership` / `review` and no dependency-gate event was written.
- GREEN after fix: dependency-gate regressions PASS; terminal-convergence `100 passed, 0 failed`; rework lifecycle `925 passed, 0 failed`; n8n validator PASS.
- `py_compile`, Ruff `E4,E7,E9,F`, and `git diff --check`: PASS.
- basedpyright: NOT RUN because no `basedpyright` executable/module is installed in the worker runtime; it was conditional-on-availability in this request.
- Durable lifecycle docs already state the required `review/done -> todo` repair for pending parents, so no canonical documentation semantic change is required.
- Deployment remains pending merge; exact live identity/read-back will be recorded after repository-owned deployment.
