# Repository registry — intake authority

This document defines the repository auto-discovery authority tracked in
issue #2.

The goal is to eliminate the permanent five-repository inventory from the
control plane. A repository opts in by carrying the GitHub topic
`hermes-agent`; the registry discovers it and derives runtime metadata instead
of requiring a source-code edit.

## Safety boundary

The registry is **read only**. `repository_registry.py` does not create/delete
webhooks, change n8n workflows, modify Hermes cron state, create Kanban boards,
spawn workers, or write to GitHub.

The registry remains read-only, but the edge intake now consumes its `ready=true`
entries as the repository inventory. The five-minute polling fallback remains
available and performs the same registry-driven full sweep.

## Derived fields and authority

For every discovered repository the registry snapshot records:

- GitHub full name and immutable repository ID
- GitHub `default_branch`
- canonical slug (`repo-name.casefold()`)
- derived checkout path `/ws/projects/<slug>`
- whether the checkout origin matches the GitHub repository
- repository contract files present on the GitHub default branch
- existing Kanban board association derived from task provenance
- readiness/fail-closed reason

The authority boundary is explicit:

```text
GitHub default branch
  └─ contract-file presence

/ws/projects/<slug>
  └─ checkout-path/origin verification only

live Kanban board DBs
  └─ existing repo → board association from tasks.idempotency_key
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

## Existing board association from durable task provenance

Board directory names are not treated as repository identity. Historical boards
may not equal the canonical repository slug, so a permanent override such as
`ctrl-hangul -> ctrlhangul` would only move the hard-coding problem.

Instead, the registry scans only direct live board databases under:

```text
/home/hermes/.hermes/kanban/boards/*/kanban.db
```

Directories beginning with `_` are ignored, which excludes `_archived` from the
resolver. State snapshots and other profiles are outside this live-board root
and therefore cannot become association evidence.

Within each live board, the resolver reads `tasks.idempotency_key` and recognizes
only the existing durable intake key format:

```text
github:<owner>/<repository>:issue:<number>
```

Resolution is fail-closed:

- exactly one repository identity on a board, and that repository appears on
  exactly one live board -> `resolved_task_provenance`
- no matching task provenance -> `not_found_task_provenance`
- one board contains provenance for multiple repositories ->
  `ambiguous_task_provenance`
- the same repository appears on multiple live boards ->
  `ambiguous_multiple_boards`

Only a verified checkout plus a uniquely resolved existing board makes an
entry `ready=true`. The live intake consumes only ready entries. The registry
still does **not** automatically provision or delete Kanban boards.

The first live evidence check found exactly one repository identity on each of
the five current boards, including recovery of the historical
`ctrl-hangul -> ctrlhangul` association without an exception table.

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

## Run or inspect the registry

Run the script where both `/ws/projects` and the live Kanban board root are
available. For the current host this is easiest inside the Hermes container
while passing the token only in the child-process environment:

```bash
docker exec -i \
  -e HERMES_GITHUB_TOKEN="$(gh auth token)" \
  hermes-cloudcli-agent \
  python3 - \
    --owner rhgo1749 \
    --topic hermes-agent \
    --checkout-root /ws/projects \
    --kanban-root /home/hermes/.hermes/kanban/boards \
  < automation/n8n/scripts/repository_registry.py
```

A fixture can be used without network access. Fixture repository objects may
include `contract_paths` so tests can model the GitHub/default-branch result.
Tests that exercise board resolution create isolated temporary SQLite board DBs
and pass their root through `--kanban-root` or the resolver helpers.


## Runtime pairing

The production intake and registry are separate scripts but one runtime unit.
`github-agent-ready-kanban-intake.py` locates the registry in this order:

1. `HERMES_REPOSITORY_REGISTRY_SCRIPT`
2. `repository_registry.py` beside the deployed intake script
3. the source-tree `automation/n8n/scripts/repository_registry.py`

A production deployment should therefore copy `repository_registry.py` beside
the intake script, or set the explicit environment path. The GitHub token is
passed to the registry only through the child-process environment and is never
placed in command-line arguments.

A scoped `--repository owner/repo` wake fails closed when the repository is not
discovered or is not ready. The no-argument fallback processes every ready
entry and reports unready entries in `registry_unready` without guessing a
board or checkout association.

## Cutover and canary gates

Before any live cutover:

1. Add `hermes-agent` only to repositories intended for Hermes management.
2. Run the shadow registry and save the JSON output outside Git.
3. Confirm the discovered repository set matches the current managed set.
4. Confirm each existing checkout reports `verified` and its remote matches.
5. Confirm contract detection matches the GitHub default branch, even if the
   local checkout is stale or on another branch.
6. Confirm each existing repository resolves to exactly one live Kanban board
   from task idempotency provenance.
7. Treat missing or ambiguous board provenance as not ready; do not guess by
   board name.
8. Keep the polling fallback available until registry-driven event routing has sufficient production evidence.

The first production shadow canary found exactly the intended five repositories
and verified all five `/ws/projects/<slug>` origins. It also exposed local
checkout drift in contract-file detection, which is why contract authority now
comes from the GitHub default branch rather than the local working tree.

The Phase-2 evidence probe then found these unique live associations from
`tasks.idempotency_key`:

```text
rhgo1749/ctrl-hangul                  -> ctrlhangul
rhgo1749/H4V3-DJ                      -> h4v3-dj
rhgo1749/h4v3-meowcore-avatar-lab     -> h4v3-meowcore-avatar-lab
rhgo1749/h4v3-meowcore-voice-lab      -> h4v3-meowcore-voice-lab
rhgo1749/re-bound                     -> re-bound
```

This is the evidence used by the registry-driven intake, so the legacy
repository-name override table is no longer required.

## Next phases

Issue #2 tracks the remaining work:

- make the edge intake repository-scoped while preserving an explicit full scan
- define provisioning behavior for opted-in repositories with no existing board
- replace per-repository n8n workflow duplication with registry-driven webhook reconciliation
- filter echo events without losing blocked-resume/rework/completion signals
- add a persisted wake lease/generation guard so stale delayed pauses are no-ops
- canary the event-driven path before retiring polling
