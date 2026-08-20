# REQ-052: superseded PR 완료 판정 보완

- Status: In Progress
- Project: `hermes-n8n-control-plane`
- Product type: `EDGE_RECONCILIATION`
- Validation profiles: `STATIC_UNIT`, `EDGE_REWORK`
- Integration target branch: `main`
- Required work branch: `fix/superseded-pr-completion`
- Source-of-truth base: `3a0c2318c040d719eeafe12a6b31bcf26a37c42f`
- Remote delivery: Required
- Pull request title/body/final report language: Korean
- Request storage: REPOSITORY_OWNED_REQUEST
- Request path: `.agent/pr-requests/REQ-052-superseded-pr-completion.md`
- Merge authority: Human/user only
- Source issue: `rhgo1749/hermes-n8n-control-plane#52`
- Source issue URL: https://github.com/rhgo1749/hermes-n8n-control-plane/issues/52
- Kanban task ID: none (manual operator patch)
- Intake idempotency key: `github:rhgo1749/hermes-n8n-control-plane:issue:52`
- Planning/lead owner: ChatGPT + user
- Implementation owner: ChatGPT + user
- Automation stop state: `NONE`

## 0. Mandatory repository route

`AGENTS.md` → `README.md` → `docs/README.md` → `docs/GITHUB_COMPLETION_LIFECYCLE.md` → `edge/kanban-github-sync.py` + focused completion regression tests.

## 1. Objective

A closed Issue whose stale closed-unmerged PR has been superseded by a newer same-head PR merged into the target branch must converge to Kanban `done` instead of repeatedly reviving as `review`.

## 2. Confirmed background

Observed `rhgo1749/H4V3-DJ#49` / task `t_23c871a3`:

- Issue #49 is closed/completed.
- PR #78 is closed/unmerged and stale.
- PR #94 is merged into `main` and uses the same task/head branch lineage.
- Current `evaluate_completion()` requires every discovered linked PR to be merged; therefore #78 causes `linked_pr_closed_not_merged` even after #94 merged.

Ownership impact: NONE. Edge remains the only GitHub↔Kanban completion projection owner. No new state/store is introduced and Hermes core is not modified.

Security/auth/network/policy impact: NONE.

## 3. In scope

1. Pass fresh source-Issue state into completion evaluation.
2. Recognize a closed-unmerged PR as superseded only when a newer linked PR with the same non-empty head ref is merged into the target branch.
3. On a closed Issue with no open linked PRs, if every otherwise-unmerged PR is proven superseded and at least one effective PR is merged into target, return `done` with reason `superseded_pr_merged`.
4. Add focused regressions and update the canonical completion lifecycle document.

## 4. Explicit non-goals

- Hermes core/Kanban schema/n8n changes.
- Generic stale-PR deletion or archival.
- Treating any merged PR as sufficient completion evidence.
- Auto-retrying/reclassifying open Issues or unrelated/different-head PRs.
- GitHub Actions changes.
- Merge/auto-merge.

## 5. Implementation requirements

- Preserve existing `all_linked_prs_merged`, `linked_pr_open`, `linked_pr_closed_not_merged`, and GitHub-query fail-closed behavior outside the narrow supersession case.
- A same-head merged PR only supersedes an older PR (`merged.number > stale.number`).
- Any open linked PR keeps the card in `review`.
- Different-head closed-unmerged PRs remain blockers.
- Source Issue must be freshly queried; only `closed` enables supersession completion.

## 6. Validation contract

Required local gates:

```bash
PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-completion.py
env -u HERMES_DELEGATED_CHILD_CONTEXT PYTHONDONTWRITEBYTECODE=1 /ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py
python3 -m py_compile edge/kanban-github-sync.py edge/test-kanban-github-sync-completion.py
git diff --check
```

Minimum regression matrix:

- closed Issue + old same-head closed/unmerged + newer same-head merged → `done/superseded_pr_merged`
- same PRs + Issue open → `review`
- closed Issue + different-head merged replacement → `review`
- closed Issue + superseded pair + additional open PR → `review`
- existing all-linked-merged and closed-unmerged behavior unchanged

## 7. Understanding handoff

Before: Issue timeline links all historical PRs → every linked PR is treated as required → one stale closed-unmerged PR can revive a completed card forever.

After: historical PRs remain visible evidence, but a narrowly proven newer same-head merged replacement can supersede an older closed-unmerged PR for a closed Issue. Edge remains authoritative and all ambiguous cases stay fail-closed/review.

## 8. Completion criteria

- [ ] Source/tests/docs updated on dedicated branch
- [ ] Focused completion regression PASS
- [ ] Existing Edge rework regression PASS
- [ ] py_compile + diff check PASS
- [ ] PR opened in Korean
- [ ] Merge remains human/user authority
