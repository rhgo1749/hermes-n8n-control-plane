#!/usr/bin/env python3
"""Deterministic regressions for the apply_decision() dependency TOCTOU guard.

Covers the race between the sync_board pre-gate and the final write:

(a) parent flips non-terminal after the pre-gate -> a stale REVIEW
    projection must be refused (card stays DONE, nothing written);
(b) a NEW parent link appears after the pre-gate -> a stale DONE
    projection must be refused (card stays REVIEW, completed_at NULL);
(c) direct done -> todo repair fixture: a stale DONE root with a running
    parent is repaired to TODO with completion/claim metadata cleared;
(d) positive controls: with no parents, and with all-terminal parents,
    the guard does not block legitimate review/done writes.

Run directly:

    python3 edge/test-kanban-github-sync-race-gate.py
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, "/ws/hermes-agent")
spec = importlib.util.spec_from_file_location("kanban_github_sync_race", ROOT / "edge" / "kanban-github-sync.py")
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

BODY = (
    "# GitHub Issue intake\n\n"
    "## Provenance\n\n"
    "- source: github-issue\n"
    "- repository: owner/repo\n"
    "- issue number: 49\n"
    "- issue title: Test issue title\n"
    "- completion contract: github-pr\n\n"
    "## Canonical Issue body\n"
)

ref = mod.GithubTaskRef("owner/repo", 49, "main", "Test issue title")
open_pr = mod.GithubPullRequest(1, "open", False, "main", "https://github.com/owner/repo/pull/1", "PR", "sha", "head", "owner", "", False)
merged_pr = mod.GithubPullRequest(1, "closed", True, "main", "https://github.com/owner/repo/pull/1", "PR", "sha", "head", "owner", "", False)
review_decision = mod.GithubCompletionDecision("review", "linked_pr_open", (1,), (open_pr,))
done_decision = mod.GithubCompletionDecision("done", "all_prs_merged", (1,), (merged_pr,))


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, body TEXT,
            completed_at INTEGER, assignee TEXT, claim_lock TEXT,
            claim_expires INTEGER, worker_pid INTEGER, block_kind TEXT,
            block_recurrences INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            run_id INTEGER, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            author TEXT, body TEXT, created_at INTEGER
        );
        """
    )
    return conn


def seed(
    child_status: str,
    parent: Optional[Tuple[str, ...]],
) -> Tuple[sqlite3.Connection, str]:
    """child = root-1 (GitHub-backed); optional parent row + link."""
    conn = db()
    conn.execute(
        "INSERT INTO tasks (id, status, body, completed_at) VALUES (?, ?, ?, ?)",
        ("root-1", child_status, BODY, 999 if child_status == "done" else None),
    )
    if parent is not None:
        conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", ("parent-1", parent[0]))
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", ("parent-1", "root-1")
        )
    conn.commit()
    return conn, "root-1"


def status_of(conn: sqlite3.Connection, task_id: str) -> str:
    return str(conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])


def events(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM task_events").fetchone()[0])


def comments(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM task_comments").fetchone()[0])


# (a) pre-gate passed, then the parent flips running: stale review projection
#     must be refused; the card stays DONE and nothing is written.
conn, root = seed("done", ("done",))
assert mod._internal_dependency_gate(conn, root)["all_terminal"]
conn.execute("UPDATE tasks SET status = 'running' WHERE id = 'parent-1'")
conn.commit()
with conn:
    result = mod.apply_decision(conn, root, review_decision)
assert result["changed"] is False, result
assert result["reason"] == "dependency_recheck_pending", result
assert result["evidence"]["parents"] == [{"id": "parent-1", "status": "running"}]
assert status_of(conn, root) == "done"
assert events(conn) == 0
assert comments(conn) == 0
print("PASS (a) parent flipped running after pre-gate: stale review write refused, card stays done")

# (b) pre-gate passed, then a NEW parent link appears: stale done projection
#     must be refused; the card stays REVIEW with completed_at NULL.
conn, root = seed("review", ("done",))
assert mod._internal_dependency_gate(conn, root)["all_terminal"]
conn.execute("INSERT INTO tasks (id, status) VALUES ('parent-2', 'running')")
conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('parent-2', 'root-1')")
conn.commit()
with conn:
    result = mod.apply_decision(conn, root, done_decision)
assert result["changed"] is False, result
assert result["reason"] == "dependency_recheck_pending", result
assert status_of(conn, root) == "review"
assert conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (root,)).fetchone()[0] is None
assert events(conn) == 0
print("PASS (b) new parent link after pre-gate: stale done write refused, card stays review")

# (c) direct done -> todo fixture: a stale DONE root with a running parent is
#     repaired to TODO with completion/claim metadata cleared, one event.
conn, root = seed("done", ("running",))
gate = mod._internal_dependency_gate(conn, root)
assert gate["pending"] == [{"id": "parent-1", "status": "running"}]
conn.execute(
    "UPDATE tasks SET completed_at = 999, assignee = 'worker', claim_lock = 'lock', "
    "claim_expires = 456, worker_pid = 789, block_kind = 'needs_input' WHERE id = ?",
    (root,),
)
with conn:
    result = mod._restore_pending_dependency(conn, root, gate)
assert result["changed"] is True and result["status"] == "todo", result
row = conn.execute(
    "SELECT status, completed_at, assignee, claim_lock, claim_expires, "
    "worker_pid, block_kind FROM tasks WHERE id = ?",
    (root,),
).fetchone()
assert tuple(row) == ("todo", None, None, None, None, None, None), tuple(row)
assert events(conn) == 1
kind = conn.execute("SELECT kind FROM task_events").fetchone()[0]
assert kind == "github_dependency_gate", kind
again = mod._restore_pending_dependency(conn, root, gate)
assert again["changed"] is False and events(conn) == 1
print("PASS (c) done -> todo direct fixture: stale done root repaired, metadata cleared, idempotent")

# (d1) positive control: no parents at all -> review write still succeeds.
conn, root = seed("done", None)
with conn:
    result = mod.apply_decision(conn, root, review_decision)
assert result["changed"] is True and result["status"] == "review", result
assert status_of(conn, root) == "review"
assert events(conn) == 1
assert comments(conn) == 1  # exactly one parking marker
second = None
with conn:
    second = mod.apply_decision(conn, root, review_decision)
assert second["changed"] is False and second["reason"] == "linked_pr_open"
assert comments(conn) == 1  # idempotent: no duplicate marker
print("PASS (d1) no parents: review projection still applies (parking marker exactly once)")

# (d2) positive control: all-terminal parents -> done write still succeeds.
conn, root = seed("review", ("done",))
with conn:
    result = mod.apply_decision(conn, root, done_decision)
assert result["changed"] is True and result["status"] == "done", result
assert status_of(conn, root) == "done"
assert conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (root,)).fetchone()[0] is not None
assert events(conn) == 1
print("PASS (d2) all-terminal parents: done projection still applies")

print("ALL RACE-GATE REGRESSIONS PASS")
