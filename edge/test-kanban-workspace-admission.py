#!/usr/bin/env python3
"""Isolated tests for the edge workspace-isolation admission overlay.

No Hermes install or real process is required: a temporary board DB with the
minimal schema exercises the guarded dispatch wrapper deterministically, with
the wrapped dispatcher and reclaim/spawn helpers patched.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

import kanban_workspace_admission as admission


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            status TEXT,
            assignee TEXT,
            workspace_kind TEXT,
            workspace_path TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            run_id INTEGER,
            kind TEXT,
            payload TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    return conn


class FakeTask:
    def __init__(self, task_id: str):
        self.id = task_id


def make_original(spawned_pid: int | None):
    def original(conn, kanban_db, board, *args, **kwargs):
        # Simulate the real dispatcher: one spawned rework worker entry.
        return [{
            "task_id": kwargs["task_id"], "status": "running", "changed": True,
            "reason": "rework_worker_spawned", "pid": spawned_pid,
        }]
    return original


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ws-admission-test-"))

    # --- unit: _isolation_violation ---
    check("shared repo root blocked",
          admission._isolation_violation("worktree", Path("/ws/projects/re-bound")) is not None)
    check("non-worktree kind blocked",
          admission._isolation_violation("dir", Path("/tmp/x")) is not None)
    check(".worktrees path allowed",
          admission._isolation_violation(
              "worktree", Path("/ws/projects/re-bound/.worktrees/t_15b405c6")) is None)

    # --- integration: guarded dispatch ---
    spec = importlib.util.spec_from_file_location(
        "edge_core", "/ws/projects/hermes-n8n-control-plane/edge/kanban-github-sync.py")
    edge = importlib.util.module_from_spec(spec)
    sys.modules["edge_core"] = edge
    # Do not execute the full module (heavy imports); stub instead.
    edge._dispatch_pending_rework = make_original(spawned_pid=None)
    admission.install_workspace_admission(edge)
    check("install wraps exactly once",
          getattr(edge._dispatch_pending_rework, "_workspace_admission_installed", False))
    admission.install_workspace_admission(edge)  # idempotent

    db = tmp / "board.db"
    conn = make_db(db)

    class FakeKB:
        @staticmethod
        def reclaim_task(conn_, task_id, reason=""):
            conn_.execute("UPDATE tasks SET status='ready' WHERE id=?", (task_id,))
            conn_.commit()
            FakeKB.reclaimed.append(task_id)
    FakeKB.reclaimed = []

    # Incident shape t_acd36c65: implementation card bound to shared checkout.
    conn.execute(
        "INSERT INTO tasks VALUES ('t_shared','구현: Issue #110 난이도','body',"
        "'running','kanban-developer','worktree','/ws/projects/re-bound')")
    out = edge._dispatch_pending_rework(
        conn, FakeKB, "re-bound", task_id="t_shared", dry_run=False)
    check("shared-checkout spawn gated", out[0]["reason"] == "workspace_isolation_violation",
          str(out))
    check("claim reclaimed", "t_shared" in FakeKB.reclaimed)
    ev = conn.execute(
        "SELECT kind FROM task_events WHERE task_id='t_shared'").fetchall()
    check("durable spawn_blocked event", any(e["kind"] == "spawn_blocked" for e in ev))
    row = conn.execute("SELECT status FROM tasks WHERE id='t_shared'").fetchone()
    check("task back to ready", row["status"] == "ready")

    # Correct isolated worktree: allowed through unchanged.
    conn.execute(
        "INSERT INTO tasks VALUES ('t_iso','REWORK round 5 fix','body',"
        "'running','kanban-developer','worktree','/ws/projects/rb/.worktrees/t_iso')")
    out2 = edge._dispatch_pending_rework(
        conn, FakeKB, "rb", task_id="t_iso", dry_run=False)
    check("isolated worktree passes", out2[0]["reason"] == "rework_worker_spawned")

    # Review card in a dir workspace: allowed (not implementation).
    conn.execute(
        "INSERT INTO tasks VALUES ('t_rev','TECH REVIEW: exact-head 검증','body',"
        "'running','kanban-reviewer','dir','/tmp/some-review-ws')")
    out3 = edge._dispatch_pending_rework(
        conn, FakeKB, "rb", task_id="t_rev", dry_run=False)
    check("review card passes", out3[0]["reason"] == "rework_worker_spawned")

    # GitHub intake card bound oddly: allowed (importer owns its binding).
    conn.execute(
        "INSERT INTO tasks VALUES ('t_intake','GitHub Issue intake: o/r#9 — bug','body',"
        "'running','kanban-main','worktree','/ws/projects/x')")
    out4 = edge._dispatch_pending_rework(
        conn, FakeKB, "x", task_id="t_intake", dry_run=False)
    check("intake card exempt", out4[0]["reason"] == "rework_worker_spawned")

    # Dry-run never gates (observation only).
    conn.execute("UPDATE tasks SET status='ready' WHERE id='t_shared'")
    out5 = edge._dispatch_pending_rework(
        conn, FakeKB, "re-bound", task_id="t_shared", dry_run=True)
    check("dry-run is observation only", out5[0]["reason"] == "rework_worker_spawned")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
