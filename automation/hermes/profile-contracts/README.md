# Kanban profile contracts

These files are repository-owned role-boundary fragments for the H4V3 Hermes Kanban profiles.

`kanban-main`, `kanban-developer`, `kanban-reviewer`, and `kanban-designer` keep their existing `SOUL.md` content. The deployer inserts or replaces one managed block delimited by:

```text
<!-- BEGIN H4V3 KANBAN INVESTIGATOR CONTRACT -->
<!-- END H4V3 KANBAN INVESTIGATOR CONTRACT -->
```

`kanban-investigator-SOUL.md` is the full canonical SOUL for the new Investigator profile. The profile directory must already exist; the deployer never clones another profile because doing so would also copy unrelated profile state.

From the repository checkout on the Ubuntu host:

```bash
python3 automation/hermes/scripts/deploy-kanban-investigator-profile-contracts.py \
  --hermes-home "$HOME/.hermes" \
  --dry-run

python3 automation/hermes/scripts/deploy-kanban-investigator-profile-contracts.py \
  --hermes-home "$HOME/.hermes"
```

The command preflights all five profiles before mutation, creates per-profile timestamped `SOUL.md` backups under `.h4v3-backups/`, writes atomically, rolls back changed SOULs if deployment fails, and is idempotent on rerun.

After deployment, restart/reload the Hermes processes that own profile prompt loading according to the current host supervisor. Do not infer live activation merely from files on disk; verify a fresh `kanban-main`/`kanban-investigator` session sees the new role boundary before treating runtime deployment as complete.

The durable role ownership contract remains `docs/KANBAN_ROLE_CONTRACTS.md`.
