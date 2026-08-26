#!/usr/bin/env python3
"""Hard workspace-isolation admission for the edge rework dispatch lane.

The pre_tool_call ``kanban-workspace-guard.py`` hook only covers cards
created through an agent session. Cards can also reach ``ready`` through the
edge's own rework intake (label-driven), through CLI edits, or through older
durable rows whose binding predates any guard. This overlay closes that gap
at the last possible chokepoint: immediately before a worker is spawned.

It wraps ``_dispatch_pending_rework`` (same seam as resource admission) and,
after a task is claimed and its workspace resolved, verifies the binding:

BLOCKS the spawn (reclaims the claim, records a durable ``spawn_blocked``
event, reports ``reason=workspace_isolation_violation``) when ALL hold:
  * the claimed row is implementation work (title/body matches the same
    implementation markers as the creation-time guard);
  * and the resolved workspace is NOT an isolated worktree:
      - kind != worktree, or
      - the path lacks a ``/.worktrees/`` segment (shared checkout), or
      - it resolves to a repository root itself (a repo-root anchor would
        mutate the shared tree).

ALLOWS everything else: review/scratch/dir workspaces on non-implementation
cards are legitimate; quarantine cards never reach this lane because they
are created blocked.

Fail-safe direction: blocking a spawn is cheap and visible (the card stays
ready with a durable event); mutating a shared checkout is expensive. DB or
filesystem errors during verification fail closed for implementation rows.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

_IMPL_RE = re.compile(
    r"(구현|REWORK|rework|IMPLEMENT|implement|bounded\s+(developer\s+)?rework"
    r"|DEV\s+Round|round[-_ ]?\d+\s*(구현|IMPLEMENT)|격리\s*개발)",
    re.IGNORECASE,
)
_INTAKE_RE = re.compile(r"GitHub Issue intake", re.IGNORECASE)

_LOG_PATH = "/home/hermes/.hermes/kanban/logs/workspace-admission.log"


def _log(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(str(entry) + "\n")
    except Exception:
        pass  # audit must never break dispatch


def _is_implementation_task(title: str, body: str) -> bool:
    hay = f"{title}\n{body or ''}"
    if _INTAKE_RE.search(hay):
        return False  # importer binds .worktrees/<task-id> deterministically
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
        dry_run = bool(kwargs.get("dry_run"))
        results = original(conn, kanban_db, board, *args, **kwargs)
        if dry_run or not isinstance(results, list):
            return list(results)  # type: ignore[return-value]

        gated: list[dict[str, Any]] = []
        for entry_any in results:
            if not isinstance(entry_any, Mapping):
                gated.append(dict(entry_any) if isinstance(entry_any, dict) else {"raw": entry_any})
                continue
            entry = dict(entry_any)
            if entry.get("reason") != "rework_worker_spawned":
                gated.append(entry)
                continue
            task_id = str(entry.get("task_id") or "")
            if not task_id:
                gated.append(entry)
                continue

            # The worker was already spawned by the wrapped call; verify the
            # durable binding it was given and stop/reclaim on violation.
            try:
                row = conn.execute(
                    "SELECT title, body, workspace_kind, workspace_path "
                    "FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
            except Exception as exc:
                _log({"ts": time.time(), "task_id": task_id,
                      "decision": "verify_error", "error": repr(exc)})
                gated.append(entry)
                continue
            if row is None:
                gated.append(entry)
                continue

            title = row["title"] or ""
            body = row["body"] or ""
            if not _is_implementation_task(title, body):
                gated.append(entry)
                continue

            kind = row["workspace_kind"]
            ws_path = row["workspace_path"] or ""
            violation = None
            try:
                violation = _isolation_violation(kind, Path(ws_path))
            except Exception as exc:  # fail closed
                violation = f"admission check failed: {type(exc).__name__}: {exc}"

            if violation is None:
                gated.append(entry)
                continue

            # HARD GATE: reclaim the just-spawned worker's claim so the
            # dispatcher never leaves a shared-checkout run alive.
            reclaim_error = None
            try:
                pid = entry.get("pid")
                if pid:
                    os.kill(int(pid), 15)  # SIGTERM the misbound worker first
            except (ProcessLookupError, ValueError, PermissionError):
                pass  # already gone; the claim release below still applies
            except Exception as exc:
                reclaim_error = repr(exc)
            try:
                kanban_db.reclaim_task(
                    conn, task_id,
                    reason=f"workspace_isolation_violation: {violation}",
                )
            except Exception as exc:
                reclaim_error = repr(exc)
            try:
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'spawn_blocked', ?, ?)",
                    (
                        task_id,
                        json.dumps(
                            {"source": "workspace_admission", "reason": violation},
                            ensure_ascii=False,
                        ),
                        time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                    ),
                )
                conn.commit()
            except Exception:
                pass  # event best-effort; reclaim above is the real gate
            _log({"ts": time.time(), "task_id": task_id, "board": board,
                  "decision": "blocked", "violation": violation})
            gated.append({
                "task_id": task_id, "status": "ready", "changed": True,
                "reason": "workspace_isolation_violation",
                "violation": violation,
                "reclaim_error": reclaim_error,
            })
        return gated

    guarded_dispatch._workspace_admission_installed = True  # type: ignore[attr-defined]
    guarded_dispatch._workspace_admission_original = original  # type: ignore[attr-defined]
    edge_module._dispatch_pending_rework = guarded_dispatch  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Self-healing pass (Issue #76)
#
# The two enforcement layers above only cover creation and spawn. Cards that
# already exist in a non-terminal state with a drifted workspace binding
# would otherwise wait for manual Main intervention. This pass runs on every
# edge wake (webhook/on-demand — no polling cron) BEFORE dispatch, so the
# repaired binding is what the dispatcher actually resolves.
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
            if (p / ".git").exists() and p.resolve(strict=False) == p.resolve(strict=False):
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


def repair_workspace_drift(
    conn: sqlite3.Connection,
    kanban_db: Any,
    board: str,
) -> list[dict[str, Any]]:
    """Deterministically rebind drifted non-terminal implementation cards.

    Returns one compact report entry per repaired/skipped card. Idempotent:
    a second run over an already-repaired board reports nothing to repair.
    """
    globals()["_current_board_slug"] = board
    repaired: list[dict[str, Any]] = []
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

        anchor = _repo_anchor_for_task(conn, row)
        if anchor is None:
            repaired.append({
                "task_id": row["id"], "board": board,
                "reason": "selfheal_anchor_unresolved",
                "violation": violation, "changed": False,
            })
            continue

        target = anchor / ".worktrees" / row["id"]
        branch = f"wt/{row['id']}"
        before = {"kind": kind, "path": ws_path, "branch": row["branch_name"]}
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=? AND status IN ('todo','ready','blocked') "
            "AND claim_lock IS NULL",
            (str(target), branch, row["id"]),
        )
        payload = json.dumps(
            {
                "source": "workspace_selfheal",
                "violation": violation,
                "before": before,
                "after": {"kind": "worktree", "path": str(target), "branch": branch},
                "anchor": str(anchor),
            },
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'workspace_repaired', ?, ?)",
            (row["id"], payload, time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"),
        )
        conn.commit()
        _log({"ts": time.time(), "board": board, "decision": "repaired",
              "task_id": row["id"], "before": before,
              "after": {"kind": "worktree", "path": str(target), "branch": branch}})
        repaired.append({
            "task_id": row["id"], "board": board,
            "reason": "workspace_selfhealed", "changed": True,
            "from": before, "to": {"kind": "worktree", "path": str(target),
                                   "branch": branch},
        })
    return repaired
