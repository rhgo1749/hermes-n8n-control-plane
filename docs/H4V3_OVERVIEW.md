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
* **Need You items**: computed projections with a one-line reason.
* **Deep links**: every board/task links to the existing Kanban tab
  (`/kanban?board=<slug>&task=<id>`); the Kanban plugin understands `board`,
  and the `task` query is retained as a provenance hint for future Kanban
  deep-link support. No Kanban UI is re-implemented.
* **Fail-closed rendering**: a board whose DB cannot be read (missing file,
  incompatible schema, locked DB) renders with a `read_error` notice and zeroed
  counts; one bad board never breaks the others or the dashboard.

### Need You projection rules

`Need You` is **not** a Kanban status. A blocked task is classified as
`need_you` only when existing Kanban/GitHub evidence says a human action is
required:

* `block_kind` is `needs_input` or `capability`;
* a task event/body carries an explicit human marker (`needs_input`,
  `needs maintainer`, `review-required`, `host_validation_required`,
  `human_validation_required`, `human review`).

A plain `blocked` without such evidence stays **Blocked** — never guessed into
Need You.

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
  tick of an incident. The dedupe key is `reason:<max-non-attention-event-id>`,
  so an unchanged incident stays quiet on every five-minute tick, while a
  **new** ordinary lifecycle event (incident resolved/recurred) permits a
  re-send.
* Rework attention keeps its existing round-aware `github_pr_rework_attention`
  writer (deduped per round/diagnostic).
* `hermes send` failures are observer-only warnings; they never fail or roll
  back reconciliation.

## Installation / update / rollback

### Install (host, Ubuntu)

```bash
automation/n8n/scripts/configure-hermes-service-auth.sh --hermes-home "$HOME/.hermes"
```

The installer copies `hermes-plugin/h4v3-overview/` to
`$HERMES_HOME/plugins/h4v3-overview/` and runs
`hermes plugins enable h4v3-overview --no-allow-tool-override` (dashboard
plugins only load when enabled). Restart the existing Hermes dashboard with
its current supervisor, then open the **H4V3 Overview** tab.

### Update

Re-run the installer after pulling the repository (files are overwritten
in place; the enablement flag is untouched).

### Rollback

```bash
rm -rf "$HERMES_HOME/plugins/h4v3-overview"
hermes plugins disable h4v3-overview
```

Restart the dashboard. No Kanban data, n8n workflows, or cron jobs are
touched by install or rollback.

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
python3 tests/test_h4v3_overview.py          # projection: counts, Need You, read-only
python3 tests/test_h4v3_notification_policy.py  # suppress/send matrix + dedupe
python3 tests/test_repo_scoped_intake.py     # intake regression
/ws/hermes-agent/venv/bin/python3 edge/test-kanban-github-sync-rework.py  # edge regression
python3 automation/n8n/scripts/validate.py   # n8n static validation
```
