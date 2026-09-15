# REQ-166: GitHub-backed root specialist join hotfix

- Status: Implementation
- Project: `hermes-n8n-control-plane`
- Product type: `CONTROL_PLANE_AUTOMATION`
- Validation profiles: `STATIC_UNIT`
- Integration target branch: `main`
- Required work branch: `hotfix/166-root-specialist-join`
- Source-of-truth base: `e1e9d6f08325d3918fecb9049aa50df916f45722`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-166-root-specialist-join.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#166`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/166`
- Kanban task ID: N/A — operator hotfix created from Issue #155 runtime evidence
- Intake idempotency key: N/A
- Planning/lead owner: ChatGPT operator session
- Implementation owner: ChatGPT operator session
- Automation stop state: `HOST_VALIDATION_REQUIRED`

## 0. Mandatory repository route

Read in order: `AGENTS.md` → `README.md` → `docs/README.md` → `docs/KANBAN_ROLE_CONTRACTS.md` + `docs/GITHUB_COMPLETION_LIFECYCLE.md` → `automation/hermes/profile-contracts/kanban-main-investigator.md` → `tests/test_kanban_investigator_role.py` + existing edge dependency-gate regression.

## 1. Objective

Prevent a GitHub-backed intake root from becoming provisionally complete and parking in edge-owned `review` while its bounded specialist graph is still active, by closing the durable dependency topology from the current graph's terminal specialist back to the intake root.

## 2. Confirmed background

- Issue #155 exposed root `t_41397dad` in parked `review` while Investigator `t_54669e81` was still RUNNING and downstream Developer/Reviewer tasks existed.
- The canonical edge dependency gate is already correct: it reads direct parents with `task_links.child_id = intake_root` and refuses external `review/done` projection while any parent is non-terminal.
- The Main managed contract describes `Main -> Investigator -> Developer -> Reviewer -> Main`, but its executable dependency guidance only requires parent links between downstream specialists. It does not explicitly require the terminal specialist to be a direct parent of the existing GitHub-backed root before Main yields/completes.
- Therefore the edge saw no pending root parent and legitimately evaluated `no_linked_pr -> review`; the missing root join is the defect.

Ownership impact: `AFFECTED` — Main dependency topology contract only. Edge remains the sole GitHub completion projection owner.
New state/store introduced: `NO`
Existing authority bypassed: `NO`
Security/auth/host exposure impact: `NONE`

## 3. In scope

1. Add a fail-closed terminal-specialist → intake-root join invariant to the canonical Kanban role contract.
2. Add the same executable requirement to the deployed `kanban-main` managed profile contract, including fresh read-back before Main yields/completes.
3. Extend static regression coverage so the deployed contract cannot drift back to a specialist-only chain.
4. Preserve the existing edge dependency gate and `no_linked_pr -> review` semantics.

## 4. Explicit non-goals

- No Hermes core fork/change.
- No n8n lifecycle state.
- No second completion owner.
- No merge/auto-merge.
- No manual mutation of the already-running Issue #155 Kanban graph in this repository change.
- No weakening of existing dependency-gate tests.

## 5. Implementation requirements

- The terminal task of each bounded specialist graph (normally Reviewer, or an optional design-review terminal when that is the approved final gate) must be a direct parent of the GitHub-backed intake root before Main releases its worker slot.
- Main must use the canonical Kanban dependency mutation surface and fresh-read the resulting root dependency graph. A body/comment reference is provenance only and does not satisfy the join.
- After the join exists, Main yields via the ordinary dependency path; it must not call root `kanban_complete` while that parent is non-terminal.
- If the join cannot be created or fresh-read, fail closed and surface the lifecycle/provenance problem instead of completing the root.
- Rework graph recreation must attach the current round's new terminal specialist to the same root; already-terminal historical parents may remain historical evidence.

## 6. Validation contract

Required local validation after checkout/runtime access is restored:

```bash
python3 tests/test_kanban_investigator_role.py
python3 edge/test-kanban-github-sync-dependency-gate.py
git diff --check
```

The first command verifies the managed Main contract/deployer wording. The second preserves the existing executable edge behavior once the root join exists. `NOT RUN != PASS`; this operator session currently lacks the H4V3 runtime/local checkout path, so host execution remains a required follow-up gate before merge.
