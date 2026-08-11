# Repository registry — shadow phase

This document defines Phase 1 of the repository auto-discovery work tracked in
issue #2.

The goal is to eliminate the permanent five-repository inventory from the
control plane. A repository opts in by carrying the GitHub topic
`hermes-agent`; the registry discovers it and derives runtime metadata instead
of requiring a source-code edit.

## Safety boundary

Phase 1 is **read only**. `repository_registry.py` does not create/delete
webhooks, change n8n workflows, modify Hermes cron state, create Kanban boards,
spawn workers, or write to GitHub.

The existing production intake and its five-minute fallback remain unchanged.
The registry is intentionally a shadow observation surface until its output is
compared with the currently managed repositories.

## Derived fields and authority

For every discovered repository the shadow snapshot records:

- GitHub full name and immutable repository ID
- GitHub `default_branch`
- canonical slug (`repo-name.casefold()`)
- derived checkout path `/ws/projects/<slug>`
- whether the checkout origin matches the GitHub repository
- repository contract files present on the GitHub default branch
- board association state
- readiness/fail-closed reason

The authority boundary is explicit:

```text
GitHub default branch
  └─ contract-file presence

/ws/projects/<slug>
  └─ checkout-path/origin verification only
```

A stale or branch-diverged local checkout must not hide or invent repository
contracts. Contract files are therefore detected through the GitHub Contents API
at the repository's discovered `default_branch`, not by inspecting the local
working tree.

Contract files are detected from one shared candidate set:

```text
AGENTS.md
AGENTS_PROJECT.md
Docs/AGENTS.md
.agent/PR_REQUEST_TEMPLATE.md
```

There is no per-repository contract list.

## Checkout convention

The current Hermes runtime uses `/ws/projects/<repo-name.casefold()>` for all
managed repositories. The registry derives this path and verifies its `origin`
normalizes to the discovered GitHub `owner/repository` identity.

The path convention is not sufficient by itself: missing checkouts, unavailable
origins, and remote mismatches all fail closed. The registry does not scan and
pick an arbitrary same-origin worktree because feature/validation worktrees may
legitimately share the same remote.

## Board identity is deliberately not guessed

Historical boards may not equal the canonical repository slug. The current
production system already contains at least one historical naming exception,
so the shadow registry reports:

```text
board = null
board_status = unresolved_shadow_phase
```

until the next phase connects a resolver to existing Kanban evidence. This is
preferable to adding another permanent override table.

The cutover gate is that the resolver must reproduce all existing effective
repo → board associations before the legacy `REPOSITORIES` table is removed.

## Authentication requirement for zero-touch onboarding

The topic is an **opt-in policy**, not an authorization grant. The GitHub
credential used for discovery must be able to see future repositories and read
the candidate contract paths from their default branches.

A fine-grained PAT limited to the current five selected repositories cannot
provide the desired "add topic and done" behavior, because a newly created
repository would be invisible until the credential's repository access was
updated manually.

For zero-touch onboarding use a credential model whose repository access
includes future owner repositories (for example, a fine-grained token granted
to all owner repositories, or a GitHub App installation configured for the
required repository set). The registry still ignores repositories that do not
carry the `hermes-agent` topic.

Do not put this credential in Git, workflow exports, command history, or chat.
The script reads `HERMES_GITHUB_TOKEN` first and falls back to `GITHUB_TOKEN`.

## Run in shadow mode

Run the script where `/ws/projects` is the Hermes workspace. For the current
host this is easiest inside the Hermes container while passing the token only in
the child-process environment:

```bash
docker exec -i \
  -e HERMES_GITHUB_TOKEN="$(gh auth token)" \
  hermes-cloudcli-agent \
  python3 - \
    --owner rhgo1749 \
    --topic hermes-agent \
    --checkout-root /ws/projects \
  < automation/n8n/scripts/repository_registry.py
```

A fixture can be used without network access. Fixture repository objects may
include `contract_paths` so tests can model the GitHub/default-branch result
without reading a local working tree:

```bash
python3 automation/n8n/scripts/repository_registry.py \
  --fixture-json /path/to/repositories.json \
  --checkout-root /ws/projects
```

## Phase-1 canary

Before any live cutover:

1. Add `hermes-agent` only to repositories intended for Hermes management.
2. Run the shadow registry and save the JSON output outside Git.
3. Confirm the discovered repository set matches the current managed set.
4. Confirm each existing checkout reports `verified` and its remote matches.
5. Confirm contract detection matches the GitHub default branch, even if the local checkout is stale or on another branch.
6. Keep board association unresolved until the Kanban-backed resolver lands.
7. Do not remove the current five-repository inventory or polling fallback yet.

The first production shadow canary found exactly the intended five repositories
and verified all five `/ws/projects/<slug>` origins. It also exposed local
checkout drift in contract-file detection, which is why contract authority now
comes from the GitHub default branch rather than the local working tree.

## Next phases

Issue #2 tracks the remaining work:

- resolve existing Kanban board association without a permanent override table
- make the edge intake repository-scoped while preserving an explicit full scan
- replace per-repository n8n workflow duplication with registry-driven webhook reconciliation
- filter echo events without losing blocked-resume/rework/completion signals
- add a persisted wake lease/generation guard so stale delayed pauses are no-ops
- canary the event-driven path before retiring polling
