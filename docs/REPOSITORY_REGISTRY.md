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

## Derived fields

For every discovered repository the shadow snapshot records:

- GitHub full name and immutable repository ID
- GitHub `default_branch`
- canonical slug (`repo-name.casefold()`)
- derived checkout path `/ws/projects/<slug>`
- whether the checkout origin matches the GitHub repository
- repository contract files that actually exist locally
- board association state
- readiness/fail-closed reason

Contract files are detected from one shared candidate set:

```text
AGENTS.md
AGENTS_PROJECT.md
Docs/AGENTS.md
.agent/PR_REQUEST_TEMPLATE.md
```

There is no per-repository contract list.

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
credential used for discovery must be able to see future repositories.

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

From the Ubuntu host checkout:

```bash
HERMES_GITHUB_TOKEN=... \
python3 automation/n8n/scripts/repository_registry.py \
  --owner rhgo1749 \
  --topic hermes-agent
```

Prefer setting the token through the existing protected host environment rather
than typing it directly into shell history.

A fixture can be used without network access:

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
5. Confirm contract detection matches the files actually present in each repo.
6. Keep board association unresolved until the Kanban-backed resolver lands.
7. Do not remove the current five-repository inventory or polling fallback yet.

## Next phases

Issue #2 tracks the remaining work:

- resolve existing Kanban board association without a permanent override table
- make the edge intake repository-scoped while preserving an explicit full scan
- replace per-repository n8n workflow duplication with registry-driven webhook reconciliation
- filter echo events without losing blocked-resume/rework/completion signals
- add a persisted wake lease/generation guard so stale delayed pauses are no-ops
- canary the event-driven path before retiring polling
