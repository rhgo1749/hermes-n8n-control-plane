#!/usr/bin/env python3
"""Deterministic SQLite regressions for the GitHub intake dependency gate."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, "/ws/hermes-agent")
spec = importlib.util.spec_from_file_location("kanban_github_sync_dependency", ROOT / "edge" / "kanban-github-sync.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, completed_at INTEGER,
            assignee TEXT, claim_lock TEXT, claim_expires INTEGER,
            worker_pid INTEGER, block_kind TEXT, block_recurrences INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            run_id INTEGER, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL
        );
    """)
    return conn


def fixture(parent_status: str, child_status: str = "review") -> tuple[sqlite3.Connection, str, str]:
    conn = db()
    parent, child = "parent-1", "root-1"
    conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", (parent, parent_status))
    conn.execute(
        "INSERT INTO tasks (id, status, completed_at, assignee, claim_lock, claim_expires, worker_pid, block_kind) "
        "VALUES (?, ?, 123, 'worker', 'lock', 456, 789, 'needs_input')",
        (child, child_status),
    )
    conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, child))
    conn.commit()
    return conn, parent, child


def check_pending(parent_status: str) -> None:
    conn, _, child = fixture(parent_status)
    gate = mod._internal_dependency_gate(conn, child)
    assert gate["pending"] == [{"id": "parent-1", "status": parent_status}]
    result = mod._restore_pending_dependency(conn, child, gate)
    row = conn.execute("SELECT status, completed_at, assignee, claim_lock, block_kind FROM tasks WHERE id = ?", (child,)).fetchone()
    assert result["status"] == "todo"
    assert result["changed"] is True
    assert tuple(row) == ("todo", None, None, None, None)
    assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == 1
    again = mod._restore_pending_dependency(conn, child, gate)
    assert again["status"] == "todo" and again["changed"] is False
    assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == 1
    print(f"PASS pending parent {parent_status}: no external projection; stale root repaired once")


for status in ("todo", "ready", "running", "review", "blocked", "scheduled"):
    check_pending(status)

for terminal in ("done", "archived"):
    conn, _, child = fixture(terminal)
    gate = mod._internal_dependency_gate(conn, child)
    assert gate["all_terminal"] and not gate["pending"]
    print(f"PASS terminal parent {terminal}: external decision is eligible")

# Both terminal parent outcomes preserve the existing external contract.
ref = mod.GithubTaskRef("owner/repo", 1, "main", "test")
open_pr = mod.GithubPullRequest(1, "open", False, "main", "https://github.com/owner/repo/pull/1", "PR", "sha", "head", "owner", "", False)
merged_pr = mod.GithubPullRequest(1, "closed", True, "main", "https://github.com/owner/repo/pull/1", "PR", "sha", "head", "owner", "", False)
assert mod.evaluate_completion(ref, (open_pr,)).desired_status == "review"
assert mod.evaluate_completion(ref, (merged_pr,)).desired_status == "done"
print("PASS terminal parents + OPEN PR -> REVIEW; merged PR -> DONE")

# A dependency lookup failure is fail-closed and does not mutate the task.
conn, _, child = fixture("running")
conn.execute("DROP TABLE task_links")
try:
    mod._internal_dependency_gate(conn, child)
except sqlite3.Error:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (child,)).fetchone()
    assert row[0] == "review"
    print("PASS parent lookup failure: current status preserved")
else:
    raise AssertionError("missing task_links must fail closed")

print("ALL DEPENDENCY-GATE REGRESSIONS PASS")
