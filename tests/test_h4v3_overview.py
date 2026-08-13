"""Focused tests for the read-only H4V3 Overview projection."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hermes-plugin" / "h4v3-overview" / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location("h4v3_overview_plugin_api", MODULE_PATH)
assert spec and spec.loader
overview = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = overview
spec.loader.exec_module(overview)


def _db(path: Path, rows: list[tuple], events: Optional[list[tuple]] = None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT,
          block_kind TEXT, block_recurrences INTEGER, consecutive_failures INTEGER,
          last_failure_error TEXT, idempotency_key TEXT, body TEXT,
          created_at INTEGER NOT NULL DEFAULT 0, current_run_id INTEGER
        );
        CREATE TABLE task_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT,
          payload TEXT, created_at INTEGER
        );
        """
    )
    conn.executemany(
        "INSERT INTO tasks (id, title, status, assignee, block_kind, block_recurrences, "
        "consecutive_failures, last_failure_error, idempotency_key, body) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        events or [],
    )
    conn.commit()
    conn.close()


def test_projection_counts_and_need_you_are_read_only() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [
                ("t-ready", "Ready", "ready", "worker", None, 0, 0, None, "github:rhgo1749/a:issue:1", ""),
                ("t-run", "Run", "running", "worker", None, 0, 0, None, None, ""),
                ("t-review", "Review", "review", None, None, 0, 0, None, None, ""),
                ("t-blocked", "Input", "blocked", None, "needs_input", 0, 0, None, None, ""),
                ("t-plain", "Plain", "blocked", None, None, 0, 0, None, None, ""),
            ],
            [("t-review", "github_pr_rework", json.dumps({"reason": "agent_rework"}), 10)],
        )
        metadata = {"slug": "demo", "name": "Demo", "db_path": str(path)}
        result = overview._load_board_projection(metadata)
        assert result["counts"]["ready"] == 1
        assert result["counts"]["running"] == 1
        assert result["counts"]["review"] == 1
        assert result["counts"]["blocked"] == 2
        assert result["rework_count"] == 1
        assert [task["id"] for task in result["tasks"] if task["attention"]] == ["t-blocked"]
        ready_task = next(task for task in result["tasks"] if task["id"] == "t-ready")
        assert ready_task["kanban_url"] == "/kanban?board=demo&task=t-ready"
        assert result["repositories"] == ["rhgo1749/a"]
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1


def test_plain_blocked_is_not_need_you() -> None:
    assert overview._need_you_reason({"status": "blocked", "block_kind": None, "attention": False}) is None
    assert overview._need_you_reason({"status": "blocked", "block_kind": "capability"}) == "capability"


def test_missing_board_db_is_safe() -> None:
    result = overview._load_board_projection({"slug": "empty", "name": "Empty", "db_path": "/does/not/exist"})
    assert result["read_error"] is None
    assert result["counts"] == overview._empty_counts()


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(json.dumps({"ok": True, "tests": len(tests)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
