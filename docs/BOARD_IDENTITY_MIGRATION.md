# Board identity migration — runbook

Repository-derived intake board identity: every registry-managed GitHub intake
board takes its canonical slug and display name exclusively from GitHub
repository metadata (`repository.name.casefold()` / `repository.name`).
Static repository->board maps, board shorthand maps, `GitHub Intake` suffix
conventions, and per-board alias/allowlist entries are retired. The only
authorized cutover surface is the reviewed tool:

```text
automation/n8n/scripts/board_identity_migration.py
```

It is deployed beside the registry by
`automation/hermes/scripts/deploy-intake-edge.sh` (as
`board_identity_migration.py`). It never mutates GitHub Issues/labels/PRs and
never hard-deletes a board (legacy archival is recoverable via `boards rm`'s
default archive). No ad-hoc production SQLite writes.

## Invariants (all stages fail closed)

- non-terminal (non done/review/archived) task on a legacy board;
- mixed/ambiguous provenance (a live board whose GitHub provenance is this
  repository plus other repositories, or a canonical-slug board whose
  provenance is another repository);
- multiple legacy boards for one repository;
- a live canonical board with non-anchor tasks beside a live legacy board
  (canonical conflict);
- missing or unverified checkout when the canonical board must be created
  (it must be an existing absolute Git root whose normalized `origin` matches
  the requested repository; no `--checkout`, no legacy `default_workdir`, or
  no `--checkout-root` also fails closed);
- missing/stale/mismatched migration evidence for `transition` /
  `postcheck` / `rollback`.
- busy migration/intake lease or a legacy task/provenance change observed by
  the under-lock transition rescan;
- missing, malformed, repository-mismatched, or foreign-owned bootstrap
  intent; and
- any backup whose current file map differs from the checksum map recorded by
  `migrate` (rollback refuses all destructive work before restoring anything).

Cutover order is strictly `migration -> transition`. `transition` requires
`--require-provenance` (re-verifies the legacy task counts, provenance, and
terminal-only state against the recorded evidence) and
`--confirm-live-transition`. Every mutating migration stage and every intake
Kanban write shares the exclusive lease at
`$HERMES_INTAKE_MIGRATION_LEASE` (default:
`<boards-root>/.intake-migration.lock`); a held lease fails closed for intake,
while migration waits briefly for an in-flight writer before refusing the
stage.

## Stages

```bash
MIG=/home/hermes/.hermes/scripts/board_identity_migration.py
REPO=rhgo1749/ctrl-hangul

# 0) Deploy the reviewed tool (after the PR is accepted and merged; this task
#    must NOT deploy it).
bash automation/hermes/scripts/deploy-intake-edge.sh --hermes-home /home/hermes/.hermes

# 1) Preflight (read-only). Repeat after every change.
python3 $MIG preflight --dry-run --repository $REPO

# 2) Migrate (pre-transition, data-preserving, under the shared lease): backs up every legacy board
#    directory, creates the canonical board if it is not live (display name =
#    repository name; default_workdir = legacy default_workdir or
#    --checkout-root/<slug>), re-verifies the legacy board byte-identical,
#    and records evidence under <state-root>/<canonical-slug>.json. Idempotent.
python3 $MIG migrate --repository $REPO

# 3) Fresh registry dry-run after migration: the repository routes by provenance
#    (the populated legacy board is still live). Intake dry-run must show
#    board_provisioning=[] (bootstrap intents are scope-limited to the woken
#    repositories and the legacy board already resolves).
python3 /home/hermes/.hermes/scripts/repository_registry.py \
  --owner rhgo1749 --topic hermes-agent --checkout-root /ws/projects \
  --kanban-root /home/hermes/.hermes/kanban/boards
/home/hermes/.local/bin/hermes --board default kanban boards list --all --json

# 4) Transition (only after migrate evidence exists and re-verifies): acquires
#    the shared lease, re-scans immediately before carrying anchors, archives
#    the legacy board (recoverable, never --delete) only after a second
#    task/provenance rescan, and makes the canonical board the sole live route.
python3 $MIG transition --require-provenance --confirm-live-transition --repository $REPO

# 5) Postcheck: canonical live with the repository-derived display name,
#    legacy gone from the live set, archived legacy byte-identical to the
#    recorded backup.
python3 $MIG postcheck --require-provenance --repository $REPO

# 6) Rollback (if needed before any post-transition work lands): under the
#    shared lease, validates every backup checksum and restore target before
#    any restore/purge/removal, restores the legacy board, purges carried
#    anchors, and removes the canonical board only while it is still empty (a
#    populated canonical board fails closed). Reversible and repeat-safe.
python3 $MIG rollback --confirm-rollback --repository $REPO
```

### Idempotency anchor carry-over

The intake dedup rule is board-scoped (`tasks.idempotency_key` on the target
board). Without a carried anchor, a re-intake of an already-imported issue
(e.g. `agent-ready` #72, whose card lives on the legacy board) would create a
duplicate root card after the cutover. `transition` therefore carries one
terminal anchor task per GitHub Issue key (created with the same key, no
assignee, completed) to the canonical board before archiving the legacy board;
each successful create/terminalize pair is checkpointed in the evidence file,
and a retry reconciles the canonical DB by idempotency key if the process dies
before the checkpoint. `rollback` consumes that checkpoint and purges the
actual canonical anchor rows. The registry routing stays unambiguous (single
provenance) in every state.

## Backup / rollback guarantees

- `migrate` copies each legacy board directory to
  `<backup-root>/<slug>-<stamp>` before touching anything and verifies the
  source directory afterwards (byte-identical check).
- `transition` archives via `hermes kanban boards rm <slug>` (default archive
  to `boards/_archived/<slug>-<ts>`; never `--delete`).
- `rollback` first validates every recorded backup (or the single matching
  archived copy) against the recorded file map. It then restores from the
  validated candidates, purges checkpointed/reconciled anchors, and removes
  the canonical board only when it is still empty.
- Migration evidence is stored under `<state-root>/<canonical-slug>.json`
  (defaults: `automation/n8n/state/board-identity-migration` and
  `automation/n8n/backups/board-identity-migration`; override with
  `HERMES_BOARD_IDENTITY_STATE_ROOT` / `HERMES_BOARD_IDENTITY_BACKUP_ROOT`).

## Deployed-runtime verification plan

Run on the live runtime after the PR is accepted/merged (NOT inside this
task; no live migration/archive/deploy/GitHub mutation here):

1. Deploy the tool (`deploy-intake-edge.sh`), then confirm the deployed copy
   compiles and `--help` exits 0.
2. `preflight --dry-run` for `rhgo1749/ctrl-hangul` against the live boards
   root: expect `legacy_boards=[ctrlhangul]` (92 done + 25 archived,
   non-terminal=0) and no errors. The empty archived canonical duplicate
   (`_archived/ctrl-hangul-*`) must not participate (archived directories are
   excluded from live evidence).
3. `migrate --dry-run` -> `canonical_action=would-create` (the canonical board
   is not currently live). Then `migrate`: verify the canonical board lands
   live with display name `ctrl-hangul` and `default_workdir` equal to the
   legacy board's declared workdir, the legacy directory is byte-identical
   against the backup checksums, and the evidence file is `stage=migrated`.
4. Fresh registry dry-run after migration: the repository resolves to the
   provenance board; `board_provisioning` is empty for the migrated scope.
5. `transition` (reviewed gates): legacy board archived under
   `boards/_archived/`, anchors carried (verify the `#72` anchor card on the
   canonical board), evidence `stage=transitioned`.
6. `postcheck`: all post-transition invariants pass; the fresh registry
   dry-run sees only the canonical target.
7. Duplicate/churn spot check: an intake dry-run for an open `agent-ready`
   issue whose key already exists on the canonical board must report the
   existing card (no duplicate root); a `hermes kanban create` with the same
   idempotency key returns the anchor.
8. Rollback drill (only if the operator decides to undo before any
   post-transition work): `rollback --confirm-rollback`, then re-run
   `preflight` to confirm the pre-transition state.

## Acceptance test coverage

`tests/test_board_identity_migration.py` (25 tests, all fixture-driven, no
live mutation):

- 5-repository registry snapshot: slug/display identity derived without any
  static mapping; first-intake bootstrap intent generic;
- display identity repository-derived (intake config validation) and no
  static maps/aliases/suffix rules remaining in the sources;
- full preflight -> migrate (dry-run + real) -> rerun -> transition ->
  postcheck cycle on the live precondition (populated legacy live, empty
  canonical duplicate already archived);
- #72-class churn prevention (anchor hit, no duplicate root card; fresh
  registry dry-run `board_provisioning=[]`);
- fail-closed: non-terminal legacy, mixed provenance, canonical conflict,
  missing/non-Git/wrong-origin checkout, missing/stale/foreign evidence;
- unrelated boards untouched (`default`, other boards);
- canonical-only display normalization (already on the canonical slug, stale
  display name);
- rollback restore + rerun safety, and the populated-canonical refusal.
- partial Nth-anchor failure with durable checkpoint/retry reconciliation;
- distinct-content backup corruption rejected before rollback mutation;
- second live target-provenance board rejected by fresh postcheck; and
- shared lease rejection of an interleaved intake writer; and
- under-lock rescan rejection of an out-of-band source task change.

`tests/test_repo_scoped_intake.py` also covers the bootstrap writer contract:
repository syntax and repository-derived slug validation, verified checkout
root/`origin`, duplicate/foreign board ownership, idempotent creation, and
same-tick first-task intake.
