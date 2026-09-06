"""Focused tests for the read-only H4V3 Overview projection."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "hermes-plugin" / "h4v3-overview" / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location("h4v3_overview_plugin_api", MODULE_PATH)
assert spec and spec.loader
overview = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = overview
spec.loader.exec_module(overview)


def _db(
    path: Path,
    rows: list[tuple[Any, ...]],
    events: Optional[list[tuple[Any, ...]]] = None,
) -> None:
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


def test_semantic_attention_survives_non_attention_event_churn() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        rework = {
            "repository": "rhgo1749/re-bound",
            "issue_number": 106,
            "pr_number": 123,
            "rework_round": 1,
            "request_comment_id": 7,
        }
        provenance = {
            "source": "rework_round",
            **rework,
            "reason": "rework_human_attention",
        }
        attention_key = "rework_human_attention:" + overview._attention_ref(
            ("rhgo1749/re-bound", 106, 123, 1, 7)
        )
        events = [
            ("t-review", "github_pr_rework", json.dumps(rework), 9),
            (
                "t-review",
                "github_operator_attention",
                json.dumps({
                    "reason": "rework_human_attention",
                    "attention_key": attention_key,
                    "incident_provenance": provenance,
                }),
                10,
            ),
            (
                "t-review",
                "github_blocked_projection",
                json.dumps({"reason": "projection"}),
                11,
            ),
        ]
        events.extend(
            ("t-noise", "lifecycle", "{}", 12 + index) for index in range(200)
        )
        _db(
            path,
            [
                (
                    "t-review",
                    "Needs maintainer",
                    "review",
                    None,
                    None,
                    0,
                    0,
                    None,
                    None,
                    "",
                ),
                ("t-noise", "Noise", "ready", None, None, 0, 0, None, None, ""),
            ],
            events,
        )
        result = overview._load_board_projection(
            {"slug": "demo", "name": "Demo", "db_path": str(path)}
        )
        task = next(task for task in result["tasks"] if task["id"] == "t-review")
        assert overview._need_you_reason(task) == "rework_human_attention"


def test_semantic_attention_rearms_only_for_a_new_rework_round() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        first = {
            "repository": "rhgo1749/re-bound",
            "issue_number": 106,
            "pr_number": 123,
            "rework_round": 1,
            "request_comment_id": 7,
        }
        second = {**first, "rework_round": 2}

        def attention_payload(
            data: dict[str, Any], reason: str = "rework_human_attention"
        ) -> dict[str, Any]:
            provenance = {
                "source": "rework_round",
                **data,
                "reason": reason,
            }
            return {
                "reason": reason,
                "attention_key": reason
                + ":"
                + overview._attention_ref(
                    (
                        data["repository"],
                        data["issue_number"],
                        data["pr_number"],
                        data["rework_round"],
                        data["request_comment_id"],
                    )
                ),
                "incident_provenance": provenance,
            }

        _db(
            path,
            [
                (
                    "t-review",
                    "Needs maintainer",
                    "review",
                    None,
                    None,
                    0,
                    0,
                    None,
                    None,
                    "",
                )
            ],
            [
                ("t-review", "github_pr_rework", json.dumps(first), 9),
                (
                    "t-review",
                    "github_operator_attention",
                    json.dumps(attention_payload(first)),
                    10,
                ),
                ("t-review", "github_pr_rework", json.dumps(second), 20),
            ],
        )
        result = overview._load_board_projection(
            {"slug": "demo", "name": "Demo", "db_path": str(path)}
        )
        assert overview._need_you_reason(result["tasks"][0]) is None

        with sqlite3.connect(path) as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "t-review",
                    "github_operator_attention",
                    json.dumps(attention_payload(second)),
                    21,
                ),
            )
            conn.commit()
        result = overview._load_board_projection(
            {"slug": "demo", "name": "Demo", "db_path": str(path)}
        )
        assert overview._need_you_reason(result["tasks"][0]) == "rework_human_attention"


def test_incomplete_rework_attention_with_pr_only_stays_unresolved() -> None:
    incomplete = {
        "reason": "rework_human_attention",
        "attention_key": "rework_human_attention",
        "incident_unresolved": True,
        "incident_provenance": {
            "source": "entry_context",
            "pr_number": 123,
            "reason": "rework_human_attention",
            "incident_ref": None,
        },
    }
    assert overview._semantic_attention_key(incomplete) is None

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [
                (
                    "t-review",
                    "Needs maintainer",
                    "review",
                    None,
                    None,
                    0,
                    0,
                    None,
                    None,
                    "",
                )
            ],
            [
                (
                    "t-review",
                    "github_operator_attention",
                    json.dumps(incomplete),
                    10,
                ),
            ],
        )
        result = overview._load_board_projection(
            {"slug": "demo", "name": "Demo", "db_path": str(path)}
        )
        assert overview._need_you_reason(result["tasks"][0]) == "rework_human_attention"


def test_semantic_blocked_attention_tracks_latest_block_and_resolution() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"

        def blocked_payload(reason: str, at: int) -> dict[str, Any]:
            blocked_id = overview._attention_ref(("needs_input", reason, at))
            return {
                "reason": "needs_input",
                "attention_key": "needs_input:" + overview._attention_ref(
                    ("needs_input", "needs_input", reason, at)
                ),
                "incident_provenance": {
                    "source": "blocked_event",
                    "blocked_event_id": blocked_id,
                    "blocked_event_kind": "needs_input",
                    "blocked_event_reason": reason,
                    "blocked_event_created_at": at,
                    "block_kind": "needs_input",
                    "reason": "needs_input",
                },
            }

        first_attention = blocked_payload("first", 10)
        second_attention = blocked_payload("second", 30)
        _db(
            path,
            [
                (
                    "t-blocked",
                    "Input",
                    "blocked",
                    None,
                    "needs_input",
                    0,
                    0,
                    None,
                    None,
                    "",
                )
            ],
            [
                (
                    "t-blocked",
                    "blocked",
                    json.dumps({"kind": "needs_input", "reason": "first"}),
                    10,
                ),
                (
                    "t-blocked",
                    "github_operator_attention",
                    json.dumps(first_attention),
                    11,
                ),
                ("t-blocked", "github_blocked_projection", "{}", 12),
            ],
        )
        projection = {"slug": "demo", "name": "Demo", "db_path": str(path)}
        result = overview._load_board_projection(projection)
        assert overview._need_you_reason(result["tasks"][0]) == "needs_input"

        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE tasks SET status = 'ready', block_kind = NULL "
                "WHERE id = 't-blocked'"
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES ('t-blocked', 'github_blocked_resolved', '{}', 20)"
            )
            conn.commit()
        result = overview._load_board_projection(projection)
        assert overview._need_you_reason(result["tasks"][0]) is None

        with sqlite3.connect(path) as conn:
            conn.execute(
                "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input' "
                "WHERE id = 't-blocked'"
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "t-blocked",
                    "blocked",
                    json.dumps({"kind": "needs_input", "reason": "second"}),
                    30,
                ),
            )
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    "t-blocked",
                    "github_operator_attention",
                    json.dumps(second_attention),
                    31,
                ),
            )
            conn.commit()
        result = overview._load_board_projection(projection)
        assert overview._need_you_reason(result["tasks"][0]) == "needs_input"


def test_semantic_attention_reason_change_replaces_previous_generation() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        blocked_payload = {"kind": "needs_input", "reason": "operator input"}
        blocked_event_id = "needs_input|operator input|10"
        blocked_ref = "needs_input|needs_input|operator input|10"
        _db(
            path,
            [
                (
                    "t-blocked",
                    "Input",
                    "blocked",
                    None,
                    "needs_input",
                    0,
                    0,
                    None,
                    None,
                    "",
                )
            ],
            [
                (
                    "t-blocked",
                    "blocked",
                    json.dumps(blocked_payload),
                    10,
                ),
                (
                    "t-blocked",
                    "github_operator_attention",
                    json.dumps({
                        "reason": "needs_input",
                        "attention_key": f"needs_input:{blocked_ref}",
                        "incident_provenance": {
                            "source": "blocked_event",
                            "blocked_event_id": blocked_event_id,
                            "blocked_event_kind": "needs_input",
                            "blocked_event_reason": "operator input",
                            "blocked_event_created_at": 10,
                            "block_kind": "needs_input",
                        },
                    }),
                    11,
                ),
                (
                    "t-blocked",
                    "github_operator_attention",
                    json.dumps({
                        "reason": "capability",
                        "attention_key": f"capability:{blocked_ref}",
                        "incident_provenance": {
                            "source": "blocked_event",
                            "blocked_event_id": blocked_event_id,
                            "blocked_event_kind": "needs_input",
                            "blocked_event_reason": "operator input",
                            "blocked_event_created_at": 10,
                            "block_kind": "needs_input",
                        },
                    }),
                    12,
                ),
                (
                    "t-blocked",
                    "commented",
                    json.dumps({"reason": "needs maintainer"}),
                    13,
                ),
            ],
        )
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            evidence = overview._load_attention_events(conn, ["t-blocked"])
        assert list(evidence) == ["t-blocked"]
        current = json.loads(evidence["t-blocked"]["payload"])
        assert current["reason"] == "capability"
        assert current["attention_key"] == f"capability:{blocked_ref}"


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


def test_block_history_reachable_in_todo_and_ready_states() -> None:
    # Issue #92: the block-semantics history is a STATUS-AGNOSTIC read surface.
    # A canonical dependency block routes the task to `todo` (auto-promotable)
    # and later `ready` — it never sits in `blocked` — so the Overview must
    # expose the dependency hold distinctly in `todo`/`ready`, not only for a
    # current human `blocked` card.
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "kanban.db"
        _db(
            path,
            [
                # dependency-wait task still in the dependency `todo` path
                ("t-dep-todo", "Dep wait", "todo", "worker", "dependency", 0, 0, None, None, ""),
                # dependency-wait task after parent resolution (auto-promoted)
                ("t-dep-ready", "Dep promoted", "ready", "worker", None, 0, 0, None, None, ""),
                # a distinct human-attention hold in `blocked`
                ("t-human", "Human", "blocked", "worker", "capability", 0, 0, None, None, ""),
                # a task with no block history at all
                ("t-plain", "Plain", "ready", "worker", None, 0, 0, None, None, ""),
            ],
            [
                ("t-dep-todo", "dependency_wait", json.dumps({"kind": "dependency", "reason": "waiting on parent", "source_status": "running"}), 100),
                ("t-dep-ready", "dependency_wait", json.dumps({"kind": "dependency", "reason": "waiting on parent", "source_status": "running"}), 100),
                ("t-dep-ready", "promoted", json.dumps({"status": "ready"}), 200),
                ("t-human", "blocked", json.dumps({"kind": "capability", "reason": "hard wall", "source_status": "running"}), 150),
            ],
        )
        result = overview._load_board_projection({"slug": "demo", "name": "Demo", "db_path": str(path)})
        by_id = {task["id"]: task for task in result["tasks"]}
        # The `todo` dependency-wait task exposes its dependency hold ...
        todo_hist = by_id["t-dep-todo"]["block_history"]
        assert todo_hist and todo_hist[0]["kind"] == "dependency_wait"
        assert todo_hist[0]["block_kind"] == "dependency"
        assert todo_hist[0]["dependency_driven"] is True
        assert todo_hist[0]["auto_promotable"] is True
        assert todo_hist[0]["reason"] == "waiting on parent"
        # ... and the `ready` (auto-promoted) task's dependency history SURVIVES
        # the promotion — it is not lost once the task leaves `blocked`-ish
        # states (the pre-fix read surface was only reachable while `blocked`).
        ready_hist = by_id["t-dep-ready"]["block_history"]
        assert ready_hist and ready_hist[0]["block_kind"] == "dependency"
        assert ready_hist[0]["dependency_driven"] is True
        assert ready_hist[0]["auto_promotable"] is True
        # A distinct human-attention hold stays a human hold, never a dependency.
        human_hist = by_id["t-human"]["block_history"]
        assert human_hist and human_hist[0]["block_kind"] == "capability"
        assert human_hist[0]["dependency_driven"] is False
        assert human_hist[0]["auto_promotable"] is False
        # A task without block events has an (explicitly empty) history.
        assert by_id["t-plain"]["block_history"] == []


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
