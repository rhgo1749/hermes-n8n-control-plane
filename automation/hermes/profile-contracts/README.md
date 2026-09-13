# Kanban profile contracts

These files are repository-owned role-boundary fragments for the H4V3 Hermes Kanban profiles.

`kanban-main`, `kanban-developer`, `kanban-reviewer`, and `kanban-designer` keep their existing `SOUL.md` content. The deployer inserts or replaces one managed block delimited by:

```text
<!-- BEGIN H4V3 KANBAN INVESTIGATOR CONTRACT -->
<!-- END H4V3 KANBAN INVESTIGATOR CONTRACT -->
```

`kanban-investigator-SOUL.md` is the full canonical SOUL for the new Investigator profile. The profile directory must already exist; the deployer never clones another profile because doing so would also copy unrelated profile state.

## Deployment target boundary

The deployer updates **one explicit Hermes profile root at a time**. Do not assume the Ubuntu host `$HOME/.hermes` is the live runtime.

In the current containerized deployment, the live Hermes runtime is `/home/hermes/.hermes` **inside `hermes-cloudcli-agent`**, matching `deploy-intake-edge.sh`. A host-side `$HOME/.hermes` should be updated only when that host profile root is intentionally used separately.

Host-side dry-run, when the host profile root itself should be checked:

```bash
python3 automation/hermes/scripts/deploy-kanban-investigator-profile-contracts.py \
  --hermes-home "$HOME/.hermes" \
  --dry-run
```

Current live container runtime, from the Ubuntu host and a checkout visible in the container:

```bash
docker exec hermes-cloudcli-agent \
  python3 /ws/projects/<checkout>/automation/hermes/scripts/deploy-kanban-investigator-profile-contracts.py \
  --hermes-home /home/hermes/.hermes \
  --dry-run

docker exec hermes-cloudcli-agent \
  python3 /ws/projects/<checkout>/automation/hermes/scripts/deploy-kanban-investigator-profile-contracts.py \
  --hermes-home /home/hermes/.hermes
```

The specialist lifecycle guard is deployed through the existing intake/edge deployer, not by copying the guard manually. The deployer renders the same approved `kanban-block-kind-guard.py` hook into both the global Hermes config and all five H4V3 profile-local `config.yaml` files (`kanban-main`, `kanban-investigator`, `kanban-developer`, `kanban-reviewer`, `kanban-designer`). This is required because profile-local hook configuration can override the otherwise-correct global hook set:

```bash
docker exec hermes-cloudcli-agent \
  bash /ws/projects/<checkout>/automation/hermes/scripts/deploy-intake-edge.sh \
  --hermes-home /home/hermes/.hermes
```

Before applying either profile-root target, inspect the live `kanban.worker_resources` policy. The runtime scheduler supports arbitrary profile names, but an operator config that enumerates exact assignee names may need `kanban-investigator` added; wildcard policies such as `kanban-*` do not.

The profile deployer preflights all five profiles before mutation, creates per-profile timestamped `SOUL.md` backups under `.h4v3-backups/`, writes atomically, rolls back changed SOULs if deployment fails, and is idempotent on rerun.

After deployment, restart/reload the Hermes processes that own profile prompt loading according to the current host supervisor. Do not infer live activation merely from files on disk; verify a fresh `kanban-main`/`kanban-investigator` session sees the new role boundary before treating runtime deployment as complete.

The durable role ownership contract remains `docs/KANBAN_ROLE_CONTRACTS.md`.
