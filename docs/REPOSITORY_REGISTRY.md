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
- resolved checkout path: the resolved board's `board.json.default_workdir` when
  declared, otherwise the legacy `/ws/projects/<slug>` fallback
- whether the checkout origin matches the GitHub repository
- repository contract files present on the GitHub default branch
- existing Kanban board association derived from task provenance, or the bounded
  first-intake canonical-board bootstrap described below
- readiness/fail-closed reason

The authority boundary is explicit:

```text
GitHub default branch
  └─ contract-file presence

live Kanban board DBs
  ├─ durable repo → board association from tasks.idempotency_key
  └─ first-intake bootstrap only: empty-provenance board named exactly <slug>

resolved board metadata
  └─ board.json.default_workdir selects checkout LOCATION only
     (repository identity is still verified from git origin)

legacy fallback when board metadata has no default_workdir
  └─ /ws/projects/<slug>
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

The checkout path is resolved only after the repository's Kanban board has been
resolved. If that live board declares an absolute `default_workdir` in
`board.json`, the registry uses that exact path and verifies its `origin`
normalizes to the discovered GitHub `owner/repository` identity. This preserves
Hermes board-owned paths such as `/ws/projects/H4V3-Meowcore` instead of
silently inventing a second case-folded checkout.

Legacy boards that do not have `board.json` or do not declare
`default_workdir` retain the historical `/ws/projects/<repo-name.casefold()>`
fallback.

Board metadata is checkout-location authority only. It does **not** establish a
repo→board association: that still comes from durable task provenance or the
bounded empty-canonical-board bootstrap. A board workdir pointing at the wrong
repository fails closed through the same origin check.

The path convention is not sufficient by itself: missing checkouts, unavailable
origins, and remote mismatches all fail closed. The registry does not scan and
pick an arbitrary same-origin worktree because feature/validation worktrees may
legitimately share the same remote. Malformed board metadata, a slug mismatch,
or a relative `default_workdir` also fails closed rather than selecting an
untrusted path.

## Existing board association from durable task provenance

Historical boards may not equal the canonical repository slug, so existing
repository→board association continues to come from durable task provenance
rather than a permanent override table. This preserves historical mappings such
as `ctrl-hangul -> ctrlhangul`.

The registry scans only direct live board databases under:

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

Normal resolution is fail-closed:

- exactly one repository identity on a board, and that repository appears on
  exactly one live board -> `resolved_task_provenance`
- one board contains provenance for multiple repositories ->
  `ambiguous_task_provenance`
- the same repository appears on multiple live boards ->
  `ambiguous_multiple_boards`

Durable task provenance always takes precedence over bootstrap naming.

## First-intake bootstrap for a new repository

A newly opted-in repository cannot have GitHub-backed task provenance before its
first Issue is admitted. Requiring that provenance unconditionally creates a
cycle:

```text
new repo -> no task provenance -> registry not ready -> intake skipped
         -> no task provenance can ever be created
```

The resolver therefore has one bounded bootstrap exception when **no durable
provenance for the target repository exists**:

1. derive the canonical slug from the GitHub repository name using
   `repo-name.casefold()`;
2. find a live board whose directory name matches that slug
   case-insensitively;
3. accept it only when that board has **no GitHub repository provenance at
   all** -> `resolved_empty_canonical_board`;
4. if the canonical board already carries provenance for another repository,
   fail closed -> `canonical_board_conflict`;
5. if multiple case-insensitive canonical boards exist, fail closed ->
   `ambiguous_canonical_boards`;
6. if no canonical board exists, remain `not_found_task_provenance`.

This is an association bootstrap, **not board provisioning**. The registry does
not create a missing board. It also never chooses a merely similar board name.

Once the first GitHub-backed Issue task is created, the next registry snapshot
sees its `github:<repo>:issue:<n>` key and returns
`resolved_task_provenance`; the naming bootstrap is no longer needed.

Only a verified checkout plus either uniquely resolved durable provenance or a
valid empty canonical-board bootstrap makes an entry `ready=true`.

The first live evidence check found exactly one repository identity on each of
the original five boards, including recovery of the historical
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
non-canonical board or checkout association.

## Cutover and canary gates

Before any live cutover:

1. Add `hermes-agent` only to repositories intended for Hermes management.
2. Run the shadow registry and save the JSON output outside Git.
3. Confirm the discovered repository set matches the current managed set.
4. Confirm each resolved board workdir (or legacy slug fallback) reports
   `verified` and its remote matches the discovered repository.
5. Confirm contract detection matches the GitHub default branch, even if the
   local checkout is stale or on another branch.
6. For established repositories, confirm each resolves to exactly one live
   Kanban board from task idempotency provenance.
7. For a first-intake repository with no provenance, confirm the only bootstrap
   candidate is an existing live board whose name equals the canonical slug and
   whose GitHub repository provenance is empty.
8. Treat canonical-board conflicts, ambiguous provenance, malformed board
   metadata, checkout remote mismatches, and missing boards as not ready; do not
   guess another board or checkout.
9. Keep the polling fallback available until registry-driven event routing has
   sufficient production evidence.

The first production shadow canary found exactly the intended five repositories
and verified all five then-current checkout origins. It also exposed local
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

Legacy associations continue to use this durable evidence. The empty-canonical
bootstrap exists only for repositories that have not produced their first
GitHub-backed task yet.

## Next phases

Issue #2 tracks the remaining work:

- define an explicit operator-owned board provisioning path for opted-in
  repositories whose canonical live board does not exist
- continue replacing per-repository n8n workflow duplication with
  registry-driven webhook reconciliation
- filter echo events without losing blocked-resume/rework/completion signals
- retain persisted wake lease/generation guards so stale delayed pauses are
  no-ops
- continue canarying the event-driven path before retiring polling

## Registry-driven GitHub webhook router

The event path contains no tracked repository inventory and no per-repository n8n GitHub Trigger workflow. The loopback `github-router` discovers `hermes-agent` repositories, reconciles one HMAC-signed repository webhook, verifies `X-Hub-Signature-256`, enqueues a durable repository/full wake scope, and wakes the existing intake through the lease controller. The intake claims one queued scope per invocation.

The five-minute n8n schedule calls `/fallback`. Webhook reconciliation failure is surfaced but does not suppress the full-registry fallback. Repository readiness remains authoritative in the registry; an event for an unready repository is safely reported/skipped until normal checkout/board provisioning makes it ready.
