#!/usr/bin/env python3
"""Hard workspace-isolation admission for the edge rework dispatch lane.

The pre_tool_call ``kanban-workspace-guard.py`` hook only covers cards
created through an agent session. Cards can also reach ``ready`` through the
edge's own rework intake (label-driven), through CLI edits, or through older
durable rows whose binding predates any guard. This overlay closes that gap
at the last possible chokepoint: BEFORE a worker is spawned.

It wraps ``_dispatch_pending_rework`` (same seam as resource admission) and
injects a GUARDED ``spawn_fn`` through to the core dispatcher. The guarded
spawn validates the claimed task's durable binding and the resolved workspace
BEFORE invoking the real spawn, so an invalid implementation binding never
invokes spawn at all (no worker process is created, nothing to SIGTERM).
Reclaim/SIGTERM remain RECOVERY-ONLY paths; they are never part of the happy
blocking path.

BLOCKS the spawn (no real spawn, claim reclaimed, durable ``spawn_blocked``
event, ``reason=workspace_isolation_violation``) when ALL hold:
  * the claimed row is implementation work (title/body matches the same
    implementation markers as the creation-time guard);
  * it is not a durable guard/quarantine card (initial_status=blocked safety
    markers never rebind — see ``_is_guard_card``);
  * and the resolved workspace is NOT an isolated worktree:
      - kind != worktree, or
      - the path lacks a ``/.worktrees/`` segment (shared checkout), or
      - it resolves to a repository root itself (a repo-root anchor would
        mutate the shared tree).

ALLOWS everything else: review/scratch/dir workspaces on non-implementation
cards are legitimate; quarantine/guard cards are excluded from this gate and
from self-heal entirely; GitHub intake cards are exempt because the importer
binds ``.worktrees/<task-id>`` deterministically.

Fail-safe direction: blocking a spawn is cheap and visible (the card stays
ready with a durable event); mutating a shared checkout is expensive. DB,
schema, read, or event-persistence errors during verification FAIL CLOSED:
the real spawn is never invoked, no claim leaks past the gate, and the
failure is surfaced as an explicit failed-gate result (never swallowed).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

_IMPL_RE = re.compile(
    r"(구현|REWORK|rework|IMPLEMENT|implement|bounded\s+(developer\s+)?rework"
    r"|DEV\s+Round|round[-_ ]?\d+\s*(구현|IMPLEMENT)|격리\s*개발)",
    re.IGNORECASE,
)
_INTAKE_RE = re.compile(r"GitHub Issue intake", re.IGNORECASE)

# Durable guard/quarantine cards (Issue #76): manual safety-guard markers that
# stay blocked forever by design. They are never self-healed nor gated — they
# keep their original shared/blocked shape until human cleanup.
_GUARD_TITLE_RE = re.compile(
    r"(SAFETY\s*GUARD|안전\s*가드|workspace\s*safety|격리수용)", re.IGNORECASE
)
_GUARD_BODY_RE = re.compile(
    r"(SAFETY\s*GUARD\s*ONLY|Never\s+dispatch|실행/구현\s*금지"
    r"|Topology\s+quarantine|quarantine\s+only)", re.IGNORECASE,
)

_LOG_PATH = "/home/hermes/.hermes/kanban/logs/workspace-admission.log"


def _log(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(str(entry) + "\n")
    except Exception:
        pass  # audit must never break dispatch


def _now() -> int:
    """Authoritative Kanban event convention: epoch INTEGER seconds."""
    return int(time.time())


def _is_guard_card(title: str, body: str) -> bool:
    """Recognize durable guard/quarantine cards by their markers.

    Guard cards carry explicit SAFETY GUARD / quarantine-only wording in
    title or body (mirrors the creation-time contract where guard cards are
    created with initial_status=blocked and must stay blocked forever).
    """
    hay = f"{title or ''}\n{body or ''}"
    if _GUARD_TITLE_RE.search(hay):
        return True
    return bool(_GUARD_BODY_RE.search(hay))


def _is_implementation_task(title: str, body: str) -> bool:
    hay = f"{title}\n{body or ''}"
    if _INTAKE_RE.search(hay):
        return False  # importer binds .worktrees/<task-id> deterministically
    if _is_guard_card(title, body):
        return False  # guard/quarantine cards sit outside both gates
    return bool(_IMPL_RE.search(hay))


def _isolation_violation(workspace_kind: Any, workspace: Path) -> str | None:
    """Return a violation reason string, or None when the binding is safe."""
    if str(workspace_kind or "") != "worktree":
        return f"workspace_kind={workspace_kind!r} is not an isolated worktree"
    parts = str(workspace).replace("\\\\", "/").split("/")
    if ".worktrees" not in parts:
        return (
            f"workspace path {workspace} has no /.worktrees/ segment — "
            "it resolves to a shared checkout"
        )
    try:
        resolved = Path(workspace).resolve(strict=False)
        # A git repository root itself is never an isolated worktree.
        probe = resolved / ".git"
        if probe.exists() and probe.is_dir():
            return (
                f"workspace path {resolved} is a repository root, not a "
                "linked worktree — spawning would mutate the shared tree"
            )
    except OSError as exc:
        return f"workspace path {workspace} unverifiable: {exc}"
    return None


def _record_spawn_blocked(
    conn: sqlite3.Connection,
    task_id: str,
    violation: str,
) -> None:
    """Persist the durable spawn_blocked event with an INTEGER timestamp."""
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'spawn_blocked', ?, ?)",
        (
            task_id,
            json.dumps(
                {"source": "workspace_admission", "reason": violation},
                ensure_ascii=False,
            ),
            _now(),
        ),
    )


class _SpawnBlocked(Exception):
    """Internal: the pre-spawn gate blocked; the real spawn was NEVER called."""

    def __init__(self, violation: str) -> None:
        super().__init__(violation)
        self.violation = violation


class WorkspaceAdmissionError(RuntimeError):
    """The overlay cannot make a safe decision."""


def install_workspace_admission(edge_module: Any) -> None:
    """Wrap ``edge_module._dispatch_pending_rework`` exactly once."""
    original = getattr(edge_module, "_dispatch_pending_rework", None)
    if not callable(original):
        raise WorkspaceAdmissionError("edge module has no _dispatch_pending_rework")
    if getattr(original, "_workspace_admission_installed", False):
        return

    def guarded_dispatch(
        conn: sqlite3.Connection,
        kanban_db: Any,
        board: str,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        # PRE-SPAWN SEAM: inject a guarded spawn_fn so invalid bindings never
        # invoke spawn. The core dispatcher prefers an injected ``spawn_fn``
        # over its default, and upstream wrappers (resource admission) forward
        # kwargs untouched, so this composes with the existing wrapper stack.
        kwargs = dict(kwargs)

        inner_spawn = kwargs.get("spawn_fn")

        def guarded_spawn(claimed: Any, workspace: Any, board: str = board) -> Any:
            return _guarded_spawn(
                conn, kanban_db, board, claimed, workspace,
                inner_spawn=inner_spawn,
                default_spawn=getattr(kanban_db, "_default_spawn", None),
            )

        kwargs["spawn_fn"] = guarded_spawn
        results = original(conn, kanban_db, board, *args, **kwargs)
        # Translate any _SpawnBlocked escape into its explicit result entry.
        if isinstance(results, list):
            sanitized: list[dict[str, Any]] = []
            for entry_any in results:
                if isinstance(entry_any, _SpawnBlocked):
                    sanitized.append(dict(entry_any.args and {} or {}) or {
                        "reason": entry_any.violation,
                    })
                    continue
                sanitized.append(entry_any)
            return sanitized
        return results

    guarded_dispatch._workspace_admission_installed = True  # type: ignore[attr-defined]
    guarded_dispatch._workspace_admission_original = original  # type: ignore[attr-defined]
    edge_module._dispatch_pending_rework = guarded_dispatch  # type: ignore[assignment]


def _guarded_spawn(
    conn: sqlite3.Connection,
    kanban_db: Any,
    board: str,
    claimed: Any,
    workspace: Any,
    *,
    inner_spawn: Any,
    default_spawn: Any,
) -> Any:
    """Validate the claimed task + resolved workspace BEFORE any spawn.

    Returns whatever the real spawn returns on allow.
    """
    task_id = str(getattr(claimed, "id", "") or "")
    if not task_id:
        # Nothing identifiable to verify — fail closed without spawning.
        raise _blocked_failure(
            conn, kanban_db, board, "", "(unidentifiable claim)",
            "admission could not identify the claimed task",
        )
    try:
        row = conn.execute(
            "SELECT title, body, workspace_kind, workspace_path "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except Exception as exc:
        raise _blocked_failure(
            conn, kanban_db, board, task_id,
            f"admission verification DB read failed: {type(exc).__name__}: {exc}",
            reclaim=False,
        ) from exc
    if row is None:
        raise _blocked_failure(
            conn, kanban_db, board, task_id,
            f"admission verification lost the claimed row for {task_id}",
        )

    title = row["title"] or ""
    body = row["body"] or ""
    kind = row["workspace_kind"]

    violation: str | None = None
    try:
        if not _is_implementation_task(title, body):
            violation = None  # review/intake/guard card: allowed through
        else:
            resolved_ws = Path(str(workspace)) if str(workspace) else Path(".")
            try:
                violation = _isolation_violation(kind, resolved_ws)
            except Exception as exc:  # fail closed on fs errors too
                violation = f"admission check failed: {type(exc).__name__}: {exc}"
    except Exception as exc:  # even marker matching must never fail open
        violation = f"admission check failed: {type(exc).__name__}: {exc}"

    if violation is None:
        real_spawn = inner_spawn if inner_spawn is not None else default_spawn
        return real_spawn(claimed, str(workspace), board=board)

    raise _blocked_failure(conn, kanban_db, board, task_id, violation)


def _blocked_failure(
    conn: sqlite3.Connection,
    kanban_db: Any,
    board: str,
    task_id: str,
    violation: str,
    *,
    reclaim: bool = True,
) -> _SpawnBlockedWithResult:
    block = _handle_block(
        conn, kanban_db, board, task_id, violation, reclaim=reclaim
    )
    exc = _SpawnBlockedWithResult(violation)
    exc.result = block
    return exc


class _SpawnBlockedWithResult(_SpawnBlocked):
    """_SpawnBlocked carrying its explicit gate-failure result entry."""

    def __init__(self, violation: str) -> None:
        super().__init__(violation)
        self.result: dict[str, Any] = {}


def _handle_block(
    conn: sqlite3.Connection,
    kanban_db: Any,
    board: str,
    task_id: str,
    violation: str,
    *,
    reclaim: bool,
) -> dict[str, Any]:
    """Recovery-only path: reclaim the just-taken claim, record the event.

    Persistence failures are surfaced explicitly instead of swallowed: the
    result carries ``gate_persistence='failed'`` so callers can tell a fully
    durable block from a degraded one. No spawn ever happened either way.
    """
    reclaim_error: str | None = None
    if reclaim and task_id:
        try:
            kanban_db.reclaim_task(
                conn, task_id,
                reason=f"workspace_isolation_violation: {violation}",
            )
        except Exception as exc:
            reclaim_error = repr(exc)
    persist_failed = False
    persist_error: str | None = None
    if task_id:
        try:
            _record_spawn_blocked(conn, task_id, violation)
            conn.commit()
        except Exception as exc:
            persist_failed = True
            persist_error = f"{type(exc).__name__}: {exc}"
            try:
                conn.rollback()
            except Exception:
                pass
    _log({"ts": time.time(), "task_id": task_id, "board": board,
          "decision": "blocked", "stage": "pre_spawn",
          "violation": violation, "persist_failed": persist_failed})
    result: dict[str, Any] = {
        "task_id": task_id or "(unknown)", "status": "ready",
        "changed": True, "reason": "workspace_isolation_violation",
        "violation": violation, "gate": "pre_spawn",
        "event_persisted": not persist_failed,
    }
    if reclaim_error is not None:
        result["reclaim_error"] = reclaim_error
    if persist_failed:
        result["gate_persistence"] = "failed"
        result["persistence_error"] = persist_error
    return result


# ---------------------------------------------------------------------------
# Self-healing pass (Issue #76)
#
# The enforcement layers above only cover creation and spawn. Cards that
# already exist in a non-terminal state with a drifted workspace binding
# would otherwise wait for manual Main intervention. This pass runs on every
# edge wake (webhook/on-demand — no polling cron) BEFORE dispatch, so the
# repaired binding is what the dispatcher actually resolves.
#
# ``repair_workspace_drift`` mutates (real runs); ``preview_workspace_drift``
# is its strictly READ-ONLY twin for dry-run wakes: identical detection and
# prediction, but zero UPDATE/event writes and zero spawns.
# ---------------------------------------------------------------------------

_ANCHOR_ISSUE_RE = re.compile(r"github:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+):issue:")


def _repo_anchor_for_task(conn: sqlite3.Connection, row: sqlite3.Row) -> Path | None:
    """Determine the repo anchor deterministically; None = fail closed."""
    # 1) GitHub provenance in idempotency_key -> sibling board checkout.
    key_row = conn.execute(
        "SELECT idempotency_key FROM tasks WHERE id = ?", (row["id"],)
    ).fetchone()
    key = (key_row["idempotency_key"] if key_row else "") or ""
    m = _ANCHOR_ISSUE_RE.search(key)
    if m:
        candidate = Path(f"/ws/projects/{m.group(2)}")
        if (candidate / ".git").exists():
            return candidate
    # 2) Existing repo-root-shaped binding that just lacks isolation.
    wp = str(row["workspace_path"] or "")
    if wp:
        p = Path(wp)
        try:
            if (p / ".git").exists():
                return p if (p / ".git").is_dir() else None
        except OSError:
            return None
    # 3) Board default_workdir.
    try:
        from hermes_cli import kanban_db as _kb  # type: ignore
        board_slug = globals().get("_current_board_slug")
        default_dir = ""
        try:
            meta = _kb.read_board_metadata(board_slug or None)
            default_dir = (meta.get("default_workdir") or "").strip()
        except Exception:
            default_dir = ""
        if default_dir:
            anchor = Path(default_dir).expanduser()
            if (anchor / ".git").exists():
                return anchor
    except ImportError:
        pass
    return None


def _drift_scan(conn: sqlite3.Connection, board: str) -> list[dict[str, Any]]:
    """Shared read-only detection used by both repair and dry-run preview."""
    globals()["_current_board_slug"] = board
    findings: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT id, title, body, status, workspace_kind, workspace_path, branch_name "
        "FROM tasks WHERE status IN ('todo','ready','blocked') AND claim_lock IS NULL"
    ).fetchall()
    for row in rows:
        title, body = row["title"] or "", row["body"] or ""
        if not _is_implementation_task(title, body):
            continue
        kind = row["workspace_kind"]
        ws_path = str(row["workspace_path"] or "")
        violation = None
        try:
            violation = _isolation_violation(kind, Path(ws_path)) if ws_path else (
                f"workspace_kind={kind!r} with empty path"
            )
        except Exception as exc:
            violation = f"check failed: {type(exc).__name__}: {exc}"
        if violation is None:
            continue
        findings.append({
            "row": row, "violation": violation,
            "kind": kind, "ws_path": ws_path,
        })
    return findings


def preview_workspace_drift(
    conn: sqlite3.Connection,
    board: str,
) -> list[dict[str, Any]]:
    """Read-only dry-run observation of what repair WOULD do.

    No UPDATE, no event insert, no spawn, no commit-side effects. Entries
    mirror the repair report shapes (predicted repair / predicted fail-closed
    anchor report) so operators can audit exactly what a real wake changes.
    """
    predicted: list[dict[str, Any]] = []
    for item in _drift_scan(conn, board):
        row = item["row"]
        anchor = _repo_anchor_for_task(conn, row)
        if anchor is None:
            predicted.append({
                "task_id": row["id"], "board": board,
                "reason": "selfheal_anchor_unresolved",
                "predicted": True,
                "violation": item["violation"], "changed": False,
            })
            continue
        target = anchor / ".worktrees" / row["id"]
        branch = f"wt/{row['id']}"
        predicted.append({
            "task_id": row["id"], "board": board,
            "reason": "workspace_selfhealed",
            "predicted": True,
            "would_change": True,
            "changed": False,
            "from": {
                "kind": item["kind"], "path": item["ws_path"],
                "branch": row["branch_name"],
            },
            "to": {"kind": "worktree", "path": str(target), "branch": branch},
            "violation": item["violation"],
        })
    return predicted


def repair_workspace_drift(
    conn: sqlite3.Connection,
    kanban_db: Any,
    board: str,
) -> list[dict[str, Any]]:
    """Deterministically rebind drifted non-terminal implementation cards.

    Returns one compact report entry per repaired/skipped card. Idempotent:
    a second run over an already-repaired board reports nothing to repair.

    Active-claim safety: the rebinding UPDATE carries the full CAS predicate
    (same status set + unclaimed). The event/report is emitted ONLY when
    ``rowcount == 1``; otherwise another writer claimed the row between scan
    and write, and this run skips with ``changed=False``, rolls back, and
    emits NO event — an active card's history is never touched.
    """
    globals()["_current_board_slug"] = board
    repaired: list[dict[str, Any]] = []
    for item in _drift_scan(conn, board):
        row = item["row"]
        task_id = row["id"]
        violation = item["violation"]

        anchor = _repo_anchor_for_task(conn, row)
        if anchor is None:
            repaired.append({
                "task_id": task_id, "board": board,
                "reason": "selfheal_anchor_unresolved",
                "violation": violation, "changed": False,
            })
            continue

        target = anchor / ".worktrees" / task_id
        branch = f"wt/{task_id}"
        before = {
            "kind": item["kind"], "path": item["ws_path"],
            "branch": row["branch_name"],
        }
        cur = conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=? AND status IN ('todo','ready','blocked') "
            "AND claim_lock IS NULL",
            (str(target), branch, task_id),
        )
        # CAS: only report/emit when THIS writer won the row.
        if cur.rowcount != 1:
            conn.rollback()
            _log({"ts": time.time(), "board": board, "decision": "skipped",
                  "task_id": task_id,
                  "why": "active_claim_race_lost", "rowcount": cur.rowcount})
            repaired.append({
                "task_id": task_id, "board": board,
                "reason": "selfheal_skipped_active_claim",
                "changed": False,
            })
            continue
        payload = json.dumps(
            {
                "source": "workspace_selfheal",
                "violation": violation,
                "before": before,
                "after": {"kind": "worktree", "path": str(target),
                          "branch": branch},
                "anchor": str(anchor),
            },
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'workspace_repaired', ?, ?)",
            (task_id, payload, _now()),
        )
        conn.commit()
        _log({"ts": time.time(), "board": board, "decision": "repaired",
              "task_id": task_id, "before": before,
              "after": {"kind": "worktree", "path": str(target),
                        "branch": branch}})
        repaired.append({
            "task_id": task_id, "board": board,
            "reason": "workspace_selfhealed", "changed": True,
            "from": before, "to": {"kind": "worktree", "path": str(target),
                                   "branch": branch},
        })
    return repaired
