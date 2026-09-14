from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "hermes-plugin" / "h4v3-overview" / "dashboard" / "trajectory_report.py"
SPEC = importlib.util.spec_from_file_location("h4v3_trajectory_report_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
trajectory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trajectory)

REPOSITORY = "rhgo1749/hermes-n8n-control-plane"
ISSUE_KEY = f"github:{REPOSITORY}:issue:138"


def _make_fixture(tmp_path: Path) -> tuple[Path, Path]:
    board = tmp_path / "kanban.db"
    conn = sqlite3.connect(board)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, status TEXT, assignee TEXT, created_by TEXT,
            created_at INTEGER, started_at INTEGER, completed_at INTEGER,
            project_id TEXT, idempotency_key TEXT
        );
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT,
            outcome TEXT, started_at INTEGER, ended_at INTEGER, metadata TEXT,
            summary TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT,
            payload TEXT, created_at INTEGER
        );
        """
    )
    tasks = [
        ("root", "done", "kanban-main", "kanban-main", 100, 101, 190, "project-root", ISSUE_KEY),
        ("dev", "done", "kanban-developer", "kanban-main", 110, 111, 130, "project-dev", ISSUE_KEY + ":developer:1"),
        ("review", "done", "kanban-reviewer", "kanban-main", 140, 141, 160, "project-review", ISSUE_KEY + ":reviewer:1"),
        ("investigate", "done", "kanban-investigator", "kanban-main", 115, 116, 135, "project-investigate", ISSUE_KEY + ":investigator:1"),
        # This card mentions the issue only in its source-side identity. It is
        # not linked and must never enter the report by body/title matching.
        ("poison", "done", "kanban-developer", "kanban-main", 120, 121, 125, "other", f"github:{REPOSITORY}:issue:999"),
    ]
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", tasks)
    conn.executemany(
        "INSERT INTO task_links VALUES (?, ?)",
        [("root", "dev"), ("root", "review"), ("root", "investigate")],
    )
    runs = [
        (1, "dev", "kanban-developer", "done", "completed", 111, 130, json.dumps({"worker_session_id": "session-dev"}), "implementation"),
        (2, "review", "kanban-reviewer", "done", "completed", 141, 160, json.dumps({"worker_session_id": "session-review", "verdict": "REWORK"}), "REWORK"),
        (3, "investigate", "kanban-investigator", "done", "completed", 116, 135, json.dumps({"worker_session_id": "session-investigate", "model_refresh": True}), "investigation"),
    ]
    conn.executemany("INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", runs)
    events = [
        (1, "root", None, "dependency_wait", json.dumps({"kind": "dependency", "reason": "waiting"}), 102),
        (2, "root", None, "promoted", "{}", 105),
        (3, "review", 2, "github_pr_rework", json.dumps({"repository": REPOSITORY, "pr_number": 149, "rework_round": 1}), 150),
        (4, "dev", 1, "blocked", json.dumps({"kind": "needs_input", "reason": "operator"}), 112),
        (5, "dev", 1, "unblocked", "{}", 120),
        (6, "dev", 1, "tool_failed", "{}", 122),
        (7, "dev", 1, "tool_retry", "{}", 123),
    ]
    conn.executemany("INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)", events)
    conn.commit()
    conn.close()

    profiles = tmp_path / "profiles"
    for profile, session_id, model, provider in [
        ("kanban-developer", "session-dev", "model-dev", "provider-dev"),
        ("kanban-reviewer", "session-review", "model-review", "provider-review"),
        ("kanban-investigator", "session-investigate", "model-investigator", "provider-investigator"),
    ]:
        profile_dir = profiles / profile
        profile_dir.mkdir(parents=True)
        state = sqlite3.connect(profile_dir / "state.db")
        state.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, model TEXT,
                billing_provider TEXT, input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                reasoning_tokens INTEGER, tool_call_count INTEGER
            );
            CREATE TABLE session_model_usage (
                session_id TEXT, model TEXT, billing_provider TEXT,
                input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                cache_write_tokens INTEGER, reasoning_tokens INTEGER
            );
            """
        )
        state.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, 111, 130, model, provider, 10, 20, 1, 2, 3, 4),
        )
        state.execute(
            "INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, model, provider, 10, 20, 1, 2, 3),
        )
        state.commit()
        state.close()
    return board, profiles


def _github_fixture() -> dict[str, object]:
    return {
        "issue": {
            "state": "closed",
            "full_name": REPOSITORY,
            "closed_at": "2026-09-14T00:00:00Z",
        },
        "pr": {
            "number": 149,
            "state": "closed",
            "merged_at": "2026-09-14T00:00:00Z",
            "head": {"sha": "f23000b9771772b6210593d5e611b782e88ba351"},
            "merge_commit_sha": "4043ec1bb8db4383dce822ec77d377da44bfee9b",
            "base": {"repo": {"full_name": REPOSITORY}},
        },
    }


def test_fixture_reconstructs_linked_rounds_and_separates_github_outcome(tmp_path: Path) -> None:
    board, profiles = _make_fixture(tmp_path)
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    report = trajectory.build_trajectory_report(
        board,
        board_slug="hermes-n8n-control-plane",
        repository=REPOSITORY,
        issue=138,
        profile_root=profiles,
        github_evidence=_github_fixture(),
        generated_at=200,
    )

    assert report["read_only"] is True
    assert report["identity"]["selection"]["body_or_title_matching"] is False
    assert report["counts"]["specialist_tasks"] == 4
    assert report["counts"]["task_links"] == 3
    assert report["counts"]["task_runs"] == 3
    assert report["counts"]["reviewer_verdicts"] == {"PASS": 0, "REWORK": 1, "UNKNOWN": 0}
    assert report["counts"]["reviewer_rework_count"] == 1
    assert report["counts"]["investigation_model_refresh"]["confirmed_count"] == 1
    assert report["counts"]["operator_intervention"]["block_unblock_pairs"] == 1
    assert report["usage"]["availability"] == "known"
    assert report["usage"]["totals"]["total_tokens"] == 90
    assert report["usage"]["totals"]["total_tokens_input_plus_output"] == 90
    assert report["usage"]["totals"]["tool_call_count"] == 12
    assert report["usage"]["tool_failures"]["value"] == 1
    assert report["usage"]["tool_failures"]["availability"] == "known"
    assert report["usage"]["tool_retries"]["value"] == 1
    assert report["usage"]["tool_retries"]["availability"] == "known"
    assert report["github"]["observed_pr_head_sha"] == "f23000b9771772b6210593d5e611b782e88ba351"
    assert report["github"]["merge_commit_sha"] == "4043ec1bb8db4383dce822ec77d377da44bfee9b"
    assert report["github"]["issue_closure_authoritative"] is True
    assert report["status"] == "complete"
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before
    encoded = json.dumps(report)
    assert "prompt" not in encoded.casefold()
    assert "secret" not in encoded.casefold()


def test_missing_usage_is_not_numeric_zero_and_period_is_known_only(tmp_path: Path) -> None:
    board, profiles = _make_fixture(tmp_path)
    (profiles / "kanban-reviewer" / "state.db").unlink()
    report = trajectory.build_trajectory_report(
        board,
        board_slug="hermes-n8n-control-plane",
        repository=REPOSITORY,
        issue=138,
        profile_root=profiles,
        github_evidence=_github_fixture(),
        generated_at=200,
    )
    assert report["usage"]["availability"] == "partial"
    assert report["usage"]["totals"]["total_tokens"] == 60
    assert report["usage"]["field_availability"]["total_tokens"] == "partial"
    assert report["usage"]["cost"]["availability"] == "unavailable"

    aggregate = trajectory.aggregate_trajectory_reports([report])
    assert aggregate["eligible_denominators"]["token_efficiency"] == 1
    assert aggregate["token_efficiency"]["availability"] == "known"


def test_root_discovery_uses_exact_source_key_and_not_issue_text(tmp_path: Path) -> None:
    board, _profiles = _make_fixture(tmp_path)
    assert trajectory.discover_root_task_ids(board, repository=REPOSITORY) == [("root", 138, 100), ("poison", 999, 120)]
    assert trajectory.discover_root_task_ids(board, repository="other/repository") == []


def test_bad_repository_is_rejected() -> None:
    with pytest.raises(trajectory.TrajectoryInputError):
        trajectory.build_trajectory_report(
            "/not-used", board_slug="board", repository="https://github.com/a/b", issue=1,
        )


def test_missing_telemetry_tables_are_partial_and_do_not_write(tmp_path: Path) -> None:
    board = tmp_path / "minimal.db"
    conn = sqlite3.connect(board)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, assignee TEXT, "
        "created_by TEXT, created_at INTEGER, started_at INTEGER, completed_at INTEGER, "
        "project_id TEXT, idempotency_key TEXT)"
    )
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("root", "done", "kanban-main", "kanban-main", 1, 2, 3, None, ISSUE_KEY),
    )
    conn.commit()
    conn.close()
    before = hashlib.sha256(board.read_bytes()).hexdigest()

    report = trajectory.build_trajectory_report(
        board,
        board_slug="hermes-n8n-control-plane",
        repository=REPOSITORY,
        issue=138,
        profile_root=tmp_path / "profiles-does-not-exist",
        github_evidence=_github_fixture(),
        generated_at=10,
    )

    assert report["status"] == "partial"
    assert report["source"]["kanban"]["read_only"] is True
    assert report["source"]["kanban"]["tables"]["task_runs"] == "unavailable"
    assert report["source"]["profile_state"]["availability"] == "unavailable"
    assert report["usage"]["totals"]["total_tokens"] is None
    assert report["usage"]["field_availability"]["total_tokens"] == "unavailable"
    assert report["usage"]["tool_failures"]["value"] is None
    assert report["coverage"]["diagnostics"]
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before


def test_plugin_router_registers_single_and_period_reports() -> None:
    plugin_path = MODULE_PATH.parent / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("h4v3_overview_plugin_api_test", plugin_path)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    paths = {getattr(route, "path", None) for route in getattr(plugin.router, "routes", [])}
    assert "/trajectory-report" in paths
    assert "/trajectory-report/aggregate" in paths
    assert "/trajectory-report/period" in paths
