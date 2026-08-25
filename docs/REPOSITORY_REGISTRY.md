# Repository registry — intake authority

This document defines the repository auto-discovery authority for GitHub intake.
A repository opts in by carrying the GitHub topic `hermes-agent`; the registry
discovers it and derives runtime metadata instead of requiring a permanent
source-code repository list.

## Safety boundary

`repository_registry.py` is **read-only**. It does not create/delete webhooks,
change n8n workflows, modify Hermes cron state, create Kanban boards, spawn
workers, or write to GitHub. For a verified new repository whose canonical board
does not exist yet, the registry only *declares* a missing-board bootstrap
intent; it never provisions the board itself.

The production intake is the **only mutation owner** for board provisioning. It
consumes registry entries with `ready=true` for task creation and, for the
first-intake path, idempotently provisions a missing canonical board through
the existing `hermes kanban boards create` surface before the first task is
written. Event routing is repository-scoped through `github-router`; there is
no five-minute polling workflow. A full-registry intake remains available only
as an explicit operator `/fallback` action.

## Derived fields and authority

For every discovered repository the registry records:

- GitHub full name and immutable repository ID;
- GitHub `default_branch`;
- canonical slug (`repo-name.casefold()`);
- canonical display identity (`repo-name`, the repository's own name — the ONLY
  display authority for board labels and notifications; there is no static
  board->label map, no `GitHub Intake` suffix convention, and no per-board
  alias/allowlist);
- resolved checkout path;
- whether checkout `origin` matches the GitHub repository;
- repository contract files present on the GitHub default branch;
- existing Kanban board association derived from durable task provenance or the
  bounded first-intake canonical-board bootstrap;
- a missing-board bootstrap intent (canonical board identity + verified
  checkout path) declared read-only for the intake, when no provenance and no
  canonical board exist;
- readiness/fail-closed reason.

The authority boundary is:

```text
GitHub default branch
  └─ contract-file presence

live Kanban board DBs
  ├─ durable repo -> board association from tasks.idempotency_key
  └─ first-intake bootstrap only: empty-provenance board named exactly <slug>

resolved board metadata
  └─ board.json.default_workdir selects checkout LOCATION only
     (repository identity is still verified from git origin)

legacy fallback when board metadata has no default_workdir
  └─ /ws/projects/<slug>
```

A stale or branch-diverged local checkout must not hide or invent repository
contracts. Contract candidates are therefore checked through GitHub at the
repository's discovered default branch.

Current contract candidates are:

```text
AGENTS.md
AGENTS_PROJECT.md
Docs/AGENTS.md
.agent/REQ_REQUEST_TEMPLATE.md
.agent/PR_REQUEST_TEMPLATE.md
```

There is no per-repository contract exception table.

## Checkout convention

Board resolution happens before checkout resolution. If the resolved live board
declares an absolute `default_workdir` in `board.json`, the registry uses that
exact path and independently verifies its `origin` normalizes to the discovered
GitHub `owner/repository` identity.

Legacy boards without a usable `board.json.default_workdir` retain the
historical `/ws/projects/<repo-name.casefold()>` fallback.

Board metadata selects checkout location only; it never establishes repository
identity. Missing checkouts, unavailable origins, remote mismatches, malformed
board metadata, relative workdirs, and slug mismatches all fail closed.

## Existing board association from durable task provenance

Historical board names do not always equal repository slugs, so normal
repository -> board association comes from durable task provenance.

The registry scans direct live board databases under:

```text
/home/hermes/.hermes/kanban/boards/*/kanban.db
```

Directories beginning with `_` are ignored. Within each board it recognizes
only existing intake keys of this form:

```text
github:<owner>/<repository>:issue:<number>
```

Normal resolution is fail-closed:

- exactly one repository identity on exactly one live board ->
  `resolved_task_provenance`;
- one board contains multiple repository identities ->
  `ambiguous_task_provenance`;
- the same repository appears on multiple live boards ->
  `ambiguous_multiple_boards`.

Durable task provenance always takes precedence over bootstrap naming.

## First-intake bootstrap

A new opted-in repository cannot have GitHub-backed task provenance before its
first Issue is admitted. To avoid a permanent bootstrap cycle, the resolver has
one bounded exception when no durable provenance exists for the target repo:

1. derive the canonical slug from the repository name with `casefold()`;
2. find a live board whose directory name matches that slug case-insensitively;
3. accept it only if the board currently has no GitHub repository provenance ->
   `resolved_empty_canonical_board`;
4. if that board carries another repository's provenance ->
   `canonical_board_conflict`;
5. if multiple case-insensitive canonical boards exist ->
   `ambiguous_canonical_boards`;
6. if no canonical board exists -> `not_found_task_provenance`.

This is association bootstrap only. The registry never provisions a board or
chooses a merely similar board name.

Once the first GitHub-backed task is created, its durable idempotency key takes
over on the next registry snapshot.

## Missing-board bootstrap intent

For a verified checkout with no durable task provenance and no canonical board
(`not_found_task_provenance`), the registry entry carries an explicit,
read-only bootstrap intent:

```text
bootstrap: {"board": "<canonical-slug>", "checkout": "<verified-checkout-path>"}
```

The intent is set only when the checkout `origin` is verified against the
discovered GitHub repository identity. All fail-closed board states
(`canonical_board_conflict`, `ambiguous_task_provenance`,
`ambiguous_multiple_boards`, `ambiguous_canonical_boards`) and every
provenance-resolved board keep `bootstrap: null`; a missing or mismatched
checkout also carries no intent.

The production intake is the only mutation owner. On a tick whose scope covers
the repository, it:

1. checks the live board list; if the canonical board already exists it does
   nothing (idempotent);
2. otherwise provisions it exactly once through the existing
   `hermes kanban boards create <canonical-slug> --default-workdir
   <verified-checkout>` surface and verifies the board actually landed
   (fail-closed otherwise);
3. reloads the registry snapshot so the freshly created empty canonical board
   resolves through the empty-canonical-board rule in the same tick, letting
   the first `agent-ready` task be created immediately.

After the first GitHub-backed task is created, its durable
`tasks.idempotency_key` provenance becomes the long-term association authority;
the bootstrap intent disappears and the empty-board exception is never used
again for that repository. Board creation never duplicates task lifecycle
authority: the registry keeps reading provenance only, and the intake creates
tasks through the existing `hermes kanban create` surface.

## Authentication requirement

The `hermes-agent` topic is opt-in policy, not an authorization grant. The
GitHub credential used for discovery must be able to see future intended owner
repositories and read candidate contract paths.

A token limited only to today's selected repositories cannot provide
"add topic and reconcile" onboarding for a newly created repository. Use a
credential model whose repository access covers the intended owner scope while
still letting the registry filter by topic.

Do not put the credential in Git, exported workflows, command history, or chat.
The registry reads `HERMES_GITHUB_TOKEN` first and falls back to `GITHUB_TOKEN`.

## Runtime pairing

The production intake and registry are separate scripts but one runtime unit.
`github-agent-ready-kanban-intake.py` locates the registry in this order:

1. `HERMES_REPOSITORY_REGISTRY_SCRIPT`;
2. `repository_registry.py` beside the deployed intake script;
3. the source-tree `automation/n8n/scripts/repository_registry.py`.

A production deployment should therefore copy `repository_registry.py` beside
the intake script, or set the explicit environment path.

A scoped `--repository owner/repo` wake fails closed when the repository is not
discovered or is not ready. An explicit full-registry fallback processes every
ready entry and reports unready entries in `registry_unready` without guessing
associations.

## Event-driven webhook router

The production event path contains no per-repository n8n workflow and no n8n
Schedule Trigger. One tracked private Webhook workflow handles only the PR
edge-sync signal:

```text
GitHub webhook
  -> github-router
       -> verify X-Hub-Signature-256
       -> ensure repository is currently managed
       -> pull_request close/rework -> n8n Webhook
            -> fixed edge-sync actuator
                 -> repository registry/task provenance -> board slug
                 -> kanban-github-sync.py --board <slug> --json
       -> other intake event -> enqueue repository scope
            -> lease-controller -> existing Hermes job default:bf431b2a6ba6
```

The router's webhook inventory is reconciled from the registry with:

```bash
automation/n8n/scripts/reconcile-github-router.sh
```

Run reconciliation when adding/removing the `hermes-agent` topic or repairing
webhook configuration. This operation changes GitHub webhook registration only;
it does not alter the Hermes job or Kanban state. Delivery replay
deduplication (bounded `X-GitHub-Delivery` TTL store) is part of the router
ingress, not the registry.

The authenticated `/fallback` endpoint remains an intentional operator recovery
path for a full-registry sweep. It is not scheduled automatically and is not
called by the tracked edge-sync Webhook.

## Onboarding gate for a new repository

Before expecting event-driven intake for a new repository:

1. add `hermes-agent` only to a repository intended for Hermes management;
2. ensure the registry credential can discover it;
3. ensure its canonical/existing Kanban board resolves safely — a verified
   new repository with no canonical board is provisioned automatically by the
   first intake tick (see Missing-board bootstrap intent);
4. ensure the expected checkout exists and its origin matches;
5. ensure contract detection on the GitHub default branch is correct;
6. run `automation/n8n/scripts/reconcile-github-router.sh`;
7. verify the expected signed webhook exists and is active;
8. send/observe a real GitHub event and confirm one scoped intake wake.

Treat ambiguous provenance, malformed board metadata, checkout remote mismatch,
or missing contract visibility as not ready. Do not guess. A missing canonical
board on a verified checkout is no longer a permanent not-ready condition: the
intake provisions it idempotently, and a conflicting/ambiguous canonical board
still fails closed.

## Historical associations

The earlier production evidence recovered these associations from durable task
provenance:

```text
rhgo1749/ctrl-hangul                  -> ctrlhangul
rhgo1749/H4V3-DJ                      -> h4v3-dj
rhgo1749/h4v3-meowcore-avatar-lab     -> h4v3-meowcore-avatar-lab
rhgo1749/h4v3-meowcore-voice-lab      -> h4v3-meowcore-voice-lab
rhgo1749/re-bound                     -> re-bound
```

These are evidence examples, not a source-code allowlist. Current membership is
discovered from the GitHub topic.

## Future work

Potential separately scoped work includes:

- lower-frequency automatic webhook reconciliation if its ownership and failure
  policy are explicitly approved;
- echo-event filtering without losing blocked-resume/rework/completion signals;
- continued lease/generation hardening and event-path canaries.
