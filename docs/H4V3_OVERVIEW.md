# H4V3 Hermes Overview + Human-Focused Notifications

## Role separation

```text
GitHub / n8n
      │
      ▼
Intake / Edge Reconciliation (this repository)
      │
      ├── Hermes Kanban state
      │
      ├── notification decision          → Hermes Gateway `hermes send` → Telegram
      │
      ▼
Hermes Kanban DB/API
      │
      ▼
H4V3 Overview (dashboard plugin, read-only)
```

* **Hermes core is not forked or modified.** The edge (`edge/kanban-github-sync.py`)
  and intake (`automation/hermes/scripts/github-agent-ready-kanban-intake.py`)
  remain the only reconciliation writers, with their existing optimistic
  transition / fail-closed / rework-round contracts untouched.
* The Overview is a **read-only projection** of the existing Hermes Kanban
  board databases. It never writes tasks, events, comments, board metadata,
  or its own database, and it performs no state changes, worker control, or
  Issue/PR creation.

## H4V3 Overview plugin

Installed as a user dashboard plugin (`hermes-plugin/h4v3-overview/`):

| Path | Purpose |
|---|---|
| `plugin.yaml` / `__init__.py` | Hermes plugin manifest + no-op `register` so `hermes plugins enable` works |
| `dashboard/manifest.json` | Dashboard plugin manifest — tab `/h4v3-overview` after Kanban |
| `dashboard/plugin_api.py` | Backend: mounts `/api/plugins/h4v3-overview/overview` |
| `dashboard/dist/index.js` | Mobile-first read-only overview UI (IIFE, no build step) |
| `dashboard/dist/style.css` | Overview styles (responsive) |

### What the Overview shows

* **Top summary**: Need You · Blocked · Review · Running · Ready + active worker count.
* **Board cards**: one per existing Hermes board (from `kanban_db.list_boards`),
  with status counts, rework count, provenance repositories (derived from
  `tasks.idempotency_key`, never hardcoded), and the most recent meaningful
  edge event. Board names come from `board.json` metadata.
* **Rework counts**: the board aggregate counts `github_pr_rework` events of
  **actionable tasks only** (`ready`/`running`/`review`/`blocked`) — rework on
  finished (`done`) cards is excluded so past work does not look like current
  risk. Per-task rework history is still shown on each task.
* **Recent meaningful state**: the single newest meaningful event on the
  board, selected by `(created_at, id)` across all tasks — not per-task
  iteration order.
* **Need You items**: computed projections with a one-line reason.
* **Deep links**: every board/task links to the existing Kanban tab
  (`/kanban?board=<slug>&task=<id>`); the Kanban plugin understands `board`,
  and the `task` query is retained as a provenance hint for future Kanban
  deep-link support. No Kanban UI is re-implemented.
* **Fail-closed rendering**: a board whose DB cannot be read (missing file,
  incompatible schema, locked DB) renders with a `read_error` notice and zeroed
  counts; one bad board never breaks the others or the dashboard.

### Need You projection rules

`Need You` is **not** a Kanban status. A task is classified as `need_you`
only when existing Kanban/GitHub evidence says a human action is required:

* `blocked` + `block_kind` is `needs_input` or `capability`;
* any non-terminal status (e.g. `review`) with explicit human-validation /
  maintainer-attention evidence in the durable event stream:
  `needs_input`, `needs maintainer`, `review-required`,
  `host_validation_required`, `human_validation_required`, `human review`.

`done` and `archived` tasks are never classified as `need_you`; their historical
attention events remain untouched but cannot keep a terminal task actionable.
A plain `review` or a plain `blocked` without such evidence stays in its own
bucket — never guessed into Need You. Evidence is read from `task_events`
payloads only; the static intake card body is excluded because it contains
contract prose ("keep HUMAN_VALIDATION_REQUIRED / HOST_VALIDATION_REQUIRED /
BLOCKED states honest") that would false-positive every card.
The board's 200-row recent-activity window does not expire attention evidence:
active tasks query their newest explicit human-attention event separately.
That evidence is still required to be unresolved: the existing
`github_operator_attention` `attention_key` points to the latest event cursor
excluding prior `github_operator_attention` rows, so a later lifecycle event
(for example REVIEW → READY/RUNNING) stales the prior incident. A new
attention event keyed to the new cursor makes Need You actionable again.
Legacy rework-attention rows use their event id against a lifecycle cursor that
excludes attention rows, without deleting or rewriting any history.

### Source of truth

* Overview = Hermes Kanban DB (read-only) + `board.json` metadata.
* Notification decision = edge/intake JSON results and durable
  `task_events` rows (the same evidence the edge already writes).

The Overview does not use the notification event stream as its source, and
the notification path never reads the Overview.

## Telegram notification policy

Telegram is an **action channel, not a second Kanban event log**. The intake
routes delivery through the existing Hermes messaging path (`hermes send`
→ `send_message_tool` → installed Telegram platform/Gateway config); it no
longer implements Telegram HTTP itself, so credentials stay in `~/.hermes/.env`
and are never read by this repository's scripts.

### Suppress / send matrix

| Event | Telegram |
|---|---|
| `READY → RUNNING` | suppress |
| `RUNNING → DONE` | suppress |
| `DONE → REVIEW` | suppress |
| `REVIEW → READY` | suppress |
| Normal rework rounds 1–2 | suppress |
| New card created by intake | suppress (visible on Overview; no alert) |
| Need You (`needs_input` / `capability` blocked) | send |
| Rework round ≥ 3 (`rework_threshold_exceeded`) | send |
| Rework delivery needs human attention (`rework_human_attention`) | send |
| Repeated worker/reconciliation failure (`rework_retry_blocked`, `spawn_failed`, `workspace_resolve_failed`, `dispatch_lock_failed`, …) | send |
| Lifecycle invariant violation (`lifecycle_label_conflict`, …) | send |
| Any other / future reason | **suppress (fail-closed)** |

The policy lives in `_entry_attention_reason` / `_should_notify_entry`
(intake) and `_operator_attention_reason` (edge). A future edge result that is
not explicitly classified can never silently start an alert storm.

### Deduplication

* The edge records a durable `github_operator_attention` event in the
  existing `task_events` table (no new notification DB) only for the first
  tick of an incident. The dedupe key is
  `reason:<max-event-id-excluding-operator-attention>`, so an unchanged
  incident stays quiet on every five-minute tick, while a **new** ordinary
  lifecycle event (incident resolved/recurred) permits a re-send.
* Rework attention keeps its existing round-aware `github_pr_rework_attention`
  writer (deduped per round/diagnostic).
* `hermes send` failures are observer-only warnings; they never fail or roll
  back reconciliation.

## Installation / update / rollback

### Overview plugin (separate installer)

```bash
automation/hermes/scripts/install-h4v3-overview.sh --hermes-home "$HOME/.hermes"
```

Independent from the n8n service-auth installer: `configure-hermes-service-auth.sh`
never installs the Overview, so an Overview failure cannot block service-auth
provisioning. The installer validates the backend (`py_compile`), installs the
plugin into `$HERMES_HOME/plugins/h4v3-overview/` atomically (previous version
kept as `h4v3-overview.bak-<ts>`), and runs
`hermes plugins enable h4v3-overview --no-allow-tool-override` (dashboard
plugins only load when enabled). Restart the existing Hermes dashboard with
its current supervisor, then open the **H4V3 Overview** tab.

### Intake/edge runtime deployment (verified live path)

**The live Hermes cron does not run this repository checkout.** Verified
2026-08-13 on the host: cron job `bf431b2a6ba6` (profile `default`) stores
`script: github-agent-ready-kanban-intake.py` with `workdir: null`, so the
scheduler executes `$HERMES_HOME/scripts/github-agent-ready-kanban-intake.py`
— a deployed copy whose hash matched the then-current repository `main`
exactly. The intake resolves its edge counterpart as a sibling
(`$HERMES_HOME/scripts/kanban-github-sync.py`), so the two files must be
updated together.

```bash
automation/hermes/scripts/deploy-intake-edge.sh --hermes-home "$HOME/.hermes"
```

The deploy script provides:

1. **candidate copy** into `$HERMES_HOME/scripts/.deploy-candidate-<ts>/`;
2. **validation** — `py_compile` + `--help` smoke for both candidates;
3. **atomic replace** — same-filesystem `mv` over the live files;
4. **verification** — installed SHA-256 must equal the checkout;
5. **rollback** — previous files kept as `.bak-<name>-<ts>` (matching the
   existing host convention), exact restore command printed;
6. **no cron changes** — job id, schedule, and enabled state are never
   touched.

### Update

Re-run the relevant installer after pulling the repository (files are
overwritten in place; the plugin enablement flag is untouched).

### Rollback

Overview:

```bash
mv "$HERMES_HOME/plugins/h4v3-overview.bak-<ts>" "$HERMES_HOME/plugins/h4v3-overview"
hermes plugins disable h4v3-overview   # 또는 enable 유지 후 재시작
```

Intake/edge: run the `mv` command printed by `deploy-intake-edge.sh` (restores
the timestamped backup). Kanban data, n8n workflows, and cron jobs are not
touched by either rollback.

## Fail-closed behavior

* Overview API returns `503` with a message when `kanban_db` is unavailable;
  per-board read failures render inline instead of breaking the page.
* Schema drift (missing `tasks` columns) renders the affected board as
  `read_error`; required columns are minimal and optional columns degrade to
  defaults.
* The Overview never initializes a board DB (read-only URI open only).
* Unknown notification reasons are suppressed by default.

## Tests

```bash
python3 tests/test_h4v3_overview.py          # projection: counts, terminal-aware Need You, recent event, rework, read-only
python3 tests/test_h4v3_notification_policy.py  # suppress/send matrix + dedupe
python3 tests/test_repo_scoped_intake.py     # intake regression
/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py  # edge regression
python3 automation/n8n/scripts/validate.py   # n8n static validation
bash -n automation/hermes/scripts/install-h4v3-overview.sh automation/hermes/scripts/deploy-intake-edge.sh
```
