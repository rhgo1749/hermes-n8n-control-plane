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
                ("t-review", "Review", "review", None, None, 0, 0, None, "github:rhgo1749/H4V3-DJ:issue:88", ""),
                ("t-blocked", "Input", "blocked", None, "needs_input", 0, 0, None, None, ""),
                ("t-plain", "Plain", "blocked", None, None, 0, 0, None, None, ""),
            ],
            [("t-review", "github_pr_rework", json.dumps({
                "reason": "agent_rework",
                "repository": "rhgo1749/H4V3-DJ",
                "pr_number": 144,
            }), 10)],
        )
        metadata = {"slug": "demo", "name": "Demo", "db_path": str(path)}
        result = overview._load_board_projection(metadata)
        assert result["counts"]["ready"] == 1
        assert result["counts"]["running"] == 1
        assert result["counts"]["review"] == 1
        assert result["counts"]["blocked"] == 2
        assert result["rework_count"] == 1
        review_task = next(task for task in result["tasks"] if task["id"] == "t-review")
        assert review_task["rework_pr_url"] == "https://github.com/rhgo1749/H4V3-DJ/pull/144"
        assert review_task["rework_pr_number"] == 144
        assert result["open_prs_url"] and (
            "is%3Apr%20is%3Aopen" in result["open_prs_url"]
            and "repo%3Arhgo1749%2FH4V3-DJ" in result["open_prs_url"]
        )
        assert "label%3Aagent-rework" not in result["open_prs_url"]
        assert [task["id"] for task in result["tasks"] if task["attention"]] == ["t-blocked"]
        ready_task = next(task for task in result["tasks"] if task["id"] == "t-ready")
        assert ready_task["kanban_url"] == "/kanban?board=demo&task=t-ready"
        assert result["repositories"] == ["rhgo1749/a", "rhgo1749/H4V3-DJ"]
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 1


def test_plain_blocked_is_not_need_you() -> None:
    assert overview._need_you_reason({"status": "blocked", "block_kind": "needs_input"}) == "needs_input"
    assert overview._need_you_reason({"status": "blocked", "block_kind": None, "attention": False}) is None
    assert overview._need_you_reason({"status": "blocked", "block_kind": "capability"}) == "capability"


def test_terminal_tasks_are_never_need_you_with_human_attention_evidence() -> None:
    stale_attention = {"attention": True, "attention_reason": "review-required"}
    assert overview._need_you_reason({"status": "done", **stale_attention}) is None
    assert overview._need_you_reason({"status": "archived", **stale_attention}) is None


def test_review_with_review_required_evidence_is_need_you() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [
                ("t-review-attn", "Needs device check", "review", None, None, 0, 0, None, None, ""),
                ("t-review-plain", "Plain review", "review", None, None, 0, 0, None, None, ""),
            ],
            [
                (
                    "t-review-attn",
                    "github_operator_attention",
                    json.dumps({
                        "reason": "review-required",
                        "diagnostic": "human_validation_required",
                        "attention_key": "review-required:0",
                    }),
                    200,
                ),
            ],
        )
        result = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        by_id = {task["id"]: task for task in result["tasks"]}
        # REVIEW + explicit evidence -> Need You
        assert overview._need_you_reason(by_id["t-review-attn"]) is not None
        # plain REVIEW -> not Need You
        assert overview._need_you_reason(by_id["t-review-plain"]) is None


def test_review_attention_survives_recent_event_window() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        events = [
            (
                "t-review-old",
                "github_operator_attention",
                json.dumps({"reason": "review-required"}),
                1,
            ),
        ]
        events.extend(
            ("t-noise", "lifecycle", "{}", 2 + index)
            for index in range(200)
        )
        _db(
            path,
            [
                ("t-review-old", "Old review", "review", None, None, 0, 0, None, None, ""),
                ("t-noise", "Recent activity", "ready", None, None, 0, 0, None, None, ""),
            ],
            events,
        )
        result = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        by_id = {task["id"]: task for task in result["tasks"]}
        assert by_id["t-review-old"]["attention"] is True
        assert overview._need_you_reason(by_id["t-review-old"]) == "review-required"
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 201


def test_review_attention_stales_after_ready_or_running_progress() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [
                ("t-ready", "Ready after review", "ready", None, None, 0, 0, None, None, ""),
                ("t-running", "Running after review", "running", "worker", None, 0, 0, None, None, ""),
            ],
            [
                (
                    "t-ready",
                    "github_operator_attention",
                    json.dumps({"reason": "review-required", "attention_key": "review-required:0"}),
                    1,
                ),
                (
                    "t-ready",
                    "github_pr_rework",
                    json.dumps({"previous_status": "review", "new_status": "ready", "reason": "agent_rework"}),
                    2,
                ),
                (
                    "t-running",
                    "github_operator_attention",
                    json.dumps({"reason": "human_validation_required", "attention_key": "human_validation_required:0"}),
                    3,
                ),
                (
                    "t-running",
                    "claimed",
                    json.dumps({"source_status": "ready", "new_status": "running"}),
                    4,
                ),
            ],
        )
        result = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        by_id = {task["id"]: task for task in result["tasks"]}
        assert overview._need_you_reason(by_id["t-ready"]) is None
        assert overview._need_you_reason(by_id["t-running"]) is None
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 4


def test_new_attention_incident_reactivates_need_you_after_progress() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [("t-review", "Review again", "review", None, None, 0, 0, None, None, "")],
            [
                (
                    "t-review",
                    "github_operator_attention",
                    json.dumps({"reason": "review-required", "attention_key": "review-required:0"}),
                    1,
                ),
                (
                    "t-review",
                    "github_pr_rework",
                    json.dumps({"previous_status": "review", "new_status": "ready", "reason": "agent_rework"}),
                    2,
                ),
            ],
        )
        first = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        assert overview._need_you_reason(first["tasks"][0]) is None

        # The edge's existing cursor contract identifies this as a new
        # incident: the key points at the latest non-attention event (id=2).
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = 't-review'")
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (
                    "t-review",
                    "github_operator_attention",
                    json.dumps({"reason": "review-required", "attention_key": "review-required:2"}),
                    3,
                ),
            )
            conn.commit()

        second = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        assert overview._need_you_reason(second["tasks"][0]) == "review-required"
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 3


def test_legacy_attention_event_stales_after_lifecycle_progress() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [("t-review", "Review", "ready", None, None, 0, 0, None, None, "")],
            [
                (
                    "t-review",
                    "github_pr_rework_attention",
                    json.dumps({"reason": "review-required", "diagnostic": "human_validation_required"}),
                    1,
                ),
                (
                    "t-review",
                    "github_pr_rework",
                    json.dumps({"previous_status": "review", "new_status": "ready", "reason": "agent_rework"}),
                    2,
                ),
            ],
        )
        result = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        assert overview._need_you_reason(result["tasks"][0]) is None


def test_recent_meaningful_picks_newest_event_across_tasks() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [
                ("t-old", "Old task", "blocked", None, None, 0, 0, None, None, ""),
                ("t-new", "New task", "ready", None, None, 0, 0, None, None, ""),
            ],
            [
                ("t-old", "github_pr_rework", json.dumps({"reason": "agent_rework", "rework_round": 1}), 100),
                ("t-new", "github_pr_rework", json.dumps({"reason": "agent_rework", "rework_round": 2}), 300),
                ("t-old", "github_pr_rework", json.dumps({"reason": "agent_rework", "rework_round": 1}), 50),
            ],
        )
        result = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        recent = result["recent_meaningful"]
        assert recent is not None
        assert recent["task_id"] == "t-new"
        assert recent["created_at"] == 300


def test_board_rework_counts_actionable_tasks_only() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [
                ("t-done", "Finished", "done", None, None, 0, 0, None, None, ""),
                ("t-ready", "Active", "ready", None, None, 0, 0, None, None, ""),
                ("t-blocked", "Blocked", "blocked", None, None, 0, 0, None, None, ""),
            ],
            [
                ("t-done", "github_pr_rework", json.dumps({"reason": "agent_rework"}), 100),
                ("t-ready", "github_pr_rework", json.dumps({"reason": "agent_rework"}), 200),
                ("t-blocked", "github_pr_rework", json.dumps({"reason": "agent_rework"}), 300),
            ],
        )
        result = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        # done task's rework excluded from the operational board aggregate
        assert result["rework_count"] == 2
        by_id = {task["id"]: task for task in result["tasks"]}
        assert by_id["t-done"]["rework_count"] == 1  # per-task history kept


def test_need_you_summary_through_build_overview() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [
                ("t-review-attn", "Device check", "review", None, None, 0, 0, None, None, ""),
                ("t-plain", "Plain", "blocked", None, None, 0, 0, None, None, ""),
                ("t-done-stale", "Finished", "done", None, None, 0, 0, None, None, ""),
            ],
            [
                (
                    "t-review-attn",
                    "github_operator_attention",
                    json.dumps({"reason": "human_validation_required", "attention_key": "x:1"}),
                    100,
                ),
                (
                    "t-done-stale",
                    "github_operator_attention",
                    json.dumps({"reason": "review-required", "attention_key": "stale:1"}),
                    200,
                ),
            ],
        )
        original = getattr(overview, "kanban_db")
        try:
            setattr(
                overview,
                "kanban_db",
                type(
                    "StubKanbanDb",
                    (),
                    {"list_boards": staticmethod(lambda include_archived=True: [{"slug": "demo", "name": "Demo", "db_path": str(path)}])},
                ),
            )
            payload = overview.build_overview()
        finally:
            setattr(overview, "kanban_db", original)
        assert payload["summary"]["need_you"] == 1
        assert payload["summary"]["review"] == 1
        assert payload["need_you"][0]["task"]["id"] == "t-review-attn"
        assert "t-done-stale" not in {item["task"]["id"] for item in payload["need_you"]}


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
