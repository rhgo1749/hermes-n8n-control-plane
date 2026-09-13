# REQ-160: 실제 pending parent 없는 dependency wait 재기동 루프 차단

- Status: Review
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `N8N_VALIDATE`
- Integration target branch: `main`
- Required work branch: `hotfix/160-dependency-wait-parent-gate`
- Source-of-truth base: latest fetched `origin/main`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-160-dependency-wait-parent-gate.md`
- Merge authority: Human/user only; originating operator explicitly authorized hotfix, merge, and deployment
- Source issue: `rhgo1749/hermes-n8n-control-plane#160`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/160`
- Automation stop state: `HOST_VALIDATION_REQUIRED` until deployed guard identity/read-back passes

## Objective

Fail closed before `kanban_block(kind=dependency)` when the target task has no canonical non-terminal parent. This prevents `todo -> immediate promotion -> Main respawn -> dependency_wait` loops caused by declaring a dependency wait before encoding the dependency.

## Confirmed evidence

Issue #138 root `t_7ec21f55` repeatedly spawned Main runs 713/715/716/717 while waiting for Investigator `t_f65c1857`; the Investigator->root link was not added until 22:32:29 KST. After the link existed, the repeated wait wake stopped. Current guard accepts every explicit canonical kind without checking whether `dependency` is backed by pending `task_links`.

## Implementation requirements

- Change only the canonical fail-closed block-kind guard plus focused tests/docs.
- `dependency` is valid only when `_read_projection(...)["pending"]` is non-empty.
- Zero parents and all-terminal parents both reject with rc=2 and no task mutation.
- Human hold kinds remain unchanged.
- Existing valid dependency `todo -> ready` core route remains unchanged.
- Both structured `kanban_block` and terminal literal block paths share the same check.
- Durable docs say link first, then dependency wait.

## Validation

```bash
/ws/hermes-agent/venv/bin/python3 -m pytest -q edge/test-kanban-block-kind-failclosed.py
python3 automation/n8n/scripts/validate.py
python3 -m py_compile automation/hermes/scripts/kanban-block-kind-guard-core.py
/ws/hermes-agent/venv/bin/python3 -m ruff check --select E4,E7,E9,F automation/hermes/scripts/kanban-block-kind-guard-core.py edge/test-kanban-block-kind-failclosed.py
git diff --check
```

Deploy after merge with the repository-owned `automation/hermes/scripts/deploy-intake-edge.sh --hermes-home /home/hermes/.hermes`, then verify source/deployed guard hashes and a no-parent live-safe guard probe against an isolated temporary board. No direct live-board mutation for validation.


## Validation evidence

- RED before fix: 3/3 new regressions failed because explicit `kind=dependency` returned rc=0 with zero pending parents (structured no-link, all-terminal-only, terminal command no-link).
- Focused GREEN: 4 passed (3 new fail-closed cases + existing valid dependency route).
- Full block-kind suite: 65 passed.
- n8n validator: PASS (`ok=true`, direct actuator topology unchanged).
- `py_compile`: PASS.
- Ruff `E4,E7,E9,F`: PASS.
- `git diff --check`: PASS.
- deploy script `bash -n`: PASS.
- Host deployment/read-back: pending merge.
