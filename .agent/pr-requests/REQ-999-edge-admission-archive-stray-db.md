# REQ — edge: exclude stray archive-container DB from resource-admission enumeration

## Source
- No upstream GitHub Issue; operator incident 2026-09-05 KST.
- Symptom: `ready` Kanban cards on the `ctrlhangul` and `h4v3-dj` boards never
  became `running`. Dispatcher logs showed repeated `resource admission:
  refusing claim_task` while zero workers were actually running.

## Root cause (two compounded defects)
1. **Trigger (external, diagnostic side-effect):** a stray zero-byte
   `~/.hermes/kanban/boards/_archived/kanban.db` was left directly inside the
   `_archived` archive container. It is not a board and has no tables
   (`no such table: task_runs`). It was produced by a read-only diagnostic that
   opened a write-mode `sqlite3.connect` on a glob expansion that included the
   `_archived/` directory.
2. **Latent defect (this repository):** both board-DB enumerators used a raw
   `boards_root.glob("*/kanban.db")`, which sweeps in the stray file:
   - `edge/kanban_resource_admission.py::_board_db_paths`
   - `edge/kanban_dynamic_resource.py::_all_board_db_paths`
     (the one installed into the live claim gate via
     `install_cross_board_helpers`)

   `_active_resource_workers` is fail-closed by design: any `sqlite3.Error`
   during inspection raises `ResourceAdmissionError`. Inspecting the empty file
   therefore rejected every claim on every board, and the core claim guard
   returned its `None` sentinel without incrementing `consecutive_failures`, so
   cards sat in `ready` with no run history indefinitely.

   This contradicts the documented intent in
   `docs/EDGE_WORKER_RESOURCE_ADMISSION.md`: the overlay must block only when
   the shared slot is *actually* occupied, and inspection failures must be
   visible diagnostics — never a permanent silent hold.

## Scope
- Add `_board_db_is_enumerable` to both modules: exclude internal
  underscore-prefixed containers (the archive root is `_archived`) and empty
  (zero-byte) files from the cross-board enumeration.
- Keep the fail-closed behavior for genuinely real board DBs that are
  malformed (do **not** fail open; do **not** create tables; do **not** raise
  capacity as a workaround).
- Add regression tests to both `edge/test-kanban-resource-admission.py` and
  `edge/test-kanban-dynamic-resource.py` reproducing the stray-file scenario.

## Non-goals
- No Hermes core (`/ws/hermes-agent/hermes_cli/*`) modification.
- No change to the immediate-mitigation already applied on the live host (the
  stray file was deleted; both affected cards re-claimed and are running).
- No second dispatcher, polling cron, n8n change, manual DB edit, or PID kill.
- No merge/auto-merge; human merge authority retained.

## Validation (local; GitHub Actions intentionally disabled)
- `python3 -m py_compile` on all four changed files: PASS.
- `edge/test-kanban-resource-admission.py`: 21 passed, 0 failed (incl. new
  stray-archive test).
- `edge/test-kanban-dynamic-resource.py`: 14 passed, 0 failed (incl. new
  stray-archive test).
- `edge/test-kanban-resource-busy-health.py`: 42 passed; 2 failures
  (`cloud codex backend resolves to cloud kind`, `CLI dry-run exposes
  resource_busy`) are pre-existing/environment-dependent — identical on the
  unmodified baseline (verified via `git stash`).

## Provenance
- Incident reproduced in isolation (temp `_archived/kanban.db` → identical
  `ResourceAdmissionError`) before the fix; the new regression tests lock the
  fix against re-introduction.

## Automation stop state
Branch created and pushed; PR opened for human review. No merge performed.
