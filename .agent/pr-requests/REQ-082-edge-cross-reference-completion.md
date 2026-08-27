# REQ-082: 타임라인 단순 멘션 PR의 완료 판정 제외

- Status: Ready for implementation
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `hermes-n8n-control-plane/t_5cab7cca-issue-82-edge-completion-cross-reference`
- Source-of-truth base: latest fetched `origin/main`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: `REPOSITORY_OWNED_REQUEST`
- Request path: `.agent/pr-requests/REQ-082-edge-cross-reference-completion.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#82`
- Source issue URL: `https://github.com/rhgo1749/hermes-n8n-control-plane/issues/82`
- Kanban task ID: `t_5cab7cca`
- Intake idempotency key: `issue-82-edge-cross-reference-dev`
- Planning/lead owner: `kanban-main`
- Implementation owner: `kanban-developer`
- Automation stop state: `HUMAN_VALIDATION_REQUIRED`

## Objective

`edge/kanban-github-sync.py` must not regress a completed CLOSED Issue to
`review/linked_pr_open` merely because a later PR mentions that Issue in a
`cross-referenced` timeline event. GitHub's `PullRequest.closingIssuesReferences`
must remain the sole relationship authority.

## Confirmed background and acceptance

- Latest fetched base is `origin/main` at `ca89c6eb0b276f85a2f22a74fdc183ac0927e59d`.
- Current discovery collects every same-repository cross-referenced PR and
  completion currently treats every discovered OPEN PR as required.
- Incident evidence: a CLOSED Issue with merged PR #14 that GitHub identifies
  as its closer was pulled back to review by unrelated OPEN PR #15, which only
  mentioned the Issue while closing another Issue.
- For CLOSED source Issue + merged authoritative closer + unrelated OPEN mention,
  completion remains `done` and never returns `linked_pr_open`.
- An OPEN PR whose authoritative closing relationship contains the source Issue
  remains review/blocking evidence.
- Missing, malformed, ambiguous, or failed relationship/API reads return the
  existing non-authoritative `github_query_failed` result; no state mutation or
  guessed `done` is allowed.
- Existing same-head CLOSED-Issue supersession, different-head/open-Issue
  rejection, all-linked-merged completion, single closed-unmerged review, open
  PR review authority, target-branch guards, dependency gates, and no-unneeded-
  Issue-GET behavior remain intact.

## Implementation boundary

### In scope

1. Extend the edge GitHub read path with bounded, validated
   `PullRequest.closingIssuesReferences` evidence.
2. Filter completion/rework/convergence decisions using that evidence while
   preserving compatibility for direct callers of the public evaluator API.
3. Add focused regressions for unrelated OPEN mentions, genuine OPEN closers,
   relationship lookup failure/malformed data, and preserved existing behavior.
4. Keep the request and completion lifecycle documentation aligned with the
   authoritative relationship rule.

### Explicit non-goals

- No Hermes core, Kanban schema, n8n workflow, host deployment, polling, or
  notification redesign.
- No changes to PR #81 / Issue #79 or intake guard behavior.
- No inference from PR body prose or a bare timeline event.
- No merge or auto-merge; no GitHub Actions enablement.

## Ownership and risk gates

- Ownership impact: `AFFECTED` only within the existing edge reconciliation
  owner; no new state store or dispatcher is introduced.
- Security/auth/secrets: `NONE`; reuse the existing GitHub token and bounded
  client, never persist or print it.
- External API policy: `AFFECTED`; GraphQL response shape is validated and all
  relationship uncertainty fails closed.
- Compatibility: add optional relationship evidence to the existing PR value
  object; callers without API evidence retain their existing direct-evaluator
  semantics, while the live verifier requires authoritative evidence.

## Validation and delivery

- Focused `edge/test-kanban-github-sync-completion.py` must print
  `ALL COMPLETION REGRESSIONS PASS`.
- Run affected terminal-convergence/rework tests, `py_compile`, Ruff/pyflakes
  over all touched Python files, `git diff --check`, and available LSP/type
  diagnostics. Prove the new regression fails with the pre-change behavior and
  passes after restoration; leave no sabotage edits.
- GitHub Actions are disabled by repository policy and are `NOT RUN`.
- Host deployment/runtime canary and human merge remain
  `HUMAN_VALIDATION_REQUIRED`; the PR is the implementation handoff, not merge
  evidence.
- Create exactly one Korean PR for Issue #82 with ordinary Markdown
  `Closes #82.` outside code fences/spans, then verify its title, base, head
  SHA, changed files, body, and fresh `closingIssuesReferences` evidence.
