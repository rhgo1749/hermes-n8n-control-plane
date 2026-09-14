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
            project_id TEXT, idempotency_key TEXT, title TEXT, body TEXT
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
        ("root", "done", "kanban-main", "kanban-main", 100, 101, 190, "project-root", ISSUE_KEY, "root", "root"),
        ("dev", "done", "kanban-developer", "kanban-main", 110, 111, 130, None, ISSUE_KEY + ":developer:1", "dev", "dev"),
        ("review", "done", "kanban-reviewer", "kanban-main", 140, 141, 160, "p_7f39082d", ISSUE_KEY + ":reviewer:1", "review", "review"),
        ("investigate", "done", "kanban-investigator", "kanban-main", 115, 116, 135, "p_7f39082d", ISSUE_KEY + ":investigator:1", "investigate", "investigate"),
        # The canonical identity belongs to Issue #154, but its prose mentions
        # #138. It must never enter the report by body/title matching.
        ("poison", "done", "kanban-developer", "kanban-main", 120, 121, 125, "other", f"github:{REPOSITORY}:issue:154", "Issue #138", "Issue #138"),
    ]
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", tasks)
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
        (8, "dev", 1, "future_event", "[malformed", 124),
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
            "closingIssuesReferences": {
                "nodes": [{"number": 138, "repository": {"full_name": REPOSITORY}}],
            },
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
    assert all(item.get("task_id") != "poison" for item in report["evidence"])
    scoped_projects = report["identity"]["project_identity"]["scoped_project_ids"]
    assert {item["profile"] for item in scoped_projects if item["project_id"] == "p_7f39082d"} == {"kanban-investigator", "kanban-reviewer"}
    assert report["identity"]["project_identity"]["null_project_id_task_count"] == 1
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
    assert report["freshness"] == {
        "report_generated_at": 200,
        "kanban_observed_at": 200,
        "profile_state_observed_at": 200,
        "github_fetched_at": 200,
    }
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


def test_default_fetcher_discovers_timeline_pr_and_validates_closing_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board, profiles = _make_fixture(tmp_path)
    calls: list[object] = []
    issue_data = {
        "state": "closed",
        "full_name": REPOSITORY,
        "closed_at": "2026-09-14T00:00:00Z",
    }
    pr_data = {
        "number": 149,
        "state": "closed",
        "merged_at": "2026-09-14T00:00:00Z",
        "head": {"sha": "f23000b9771772b6210593d5e611b782e88ba351"},
        "merge_commit_sha": "4043ec1bb8db4383dce822ec77d377da44bfee9b",
        "base": {"repo": {"full_name": REPOSITORY}},
    }

    def rest(path: str) -> object:
        calls.append(path)
        if path == f"/repos/{REPOSITORY}/issues/138":
            return issue_data
        if path == f"/repos/{REPOSITORY}/issues/138/timeline?per_page=100":
            return [{
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 149,
                        "pull_request": {"url": "https://api.github.com/repos/x/pulls/149"},
                    }
                },
            }]
        if path == f"/repos/{REPOSITORY}/pulls/149":
            return pr_data
        raise AssertionError(f"unexpected REST path: {path}")

    def graphql(query: str, variables: dict[str, object]) -> dict[str, object]:
        calls.append(("graphql", dict(variables)))
        assert "closingIssuesReferences" in query
        assert variables == {"owner": "rhgo1749", "name": "hermes-n8n-control-plane", "number": 149}
        return {
            "repository": {
                "pullRequest": {
                    "number": 149,
                    "closingIssuesReferences": {
                        "nodes": [{
                            "number": 138,
                            "repository": {"nameWithOwner": REPOSITORY},
                        }],
                    },
                },
            },
        }

    monkeypatch.setattr(trajectory, "_default_github_fetch", rest)
    monkeypatch.setattr(trajectory, "_default_github_graphql_fetch", graphql)
    report = trajectory.build_trajectory_report(
        board,
        board_slug="hermes-n8n-control-plane",
        repository=REPOSITORY,
        issue=138,
        task_ids=[],
        profile_root=profiles,
        generated_at=200,
    )
    assert calls == [
        f"/repos/{REPOSITORY}/issues/138",
        f"/repos/{REPOSITORY}/issues/138/timeline?per_page=100",
        f"/repos/{REPOSITORY}/pulls/149",
        ("graphql", {"owner": "rhgo1749", "name": "hermes-n8n-control-plane", "number": 149}),
    ]
    assert report["github"]["availability"] == "known"
    assert report["github"]["pr_number"] == 149
    assert report["github"]["observed_pr_head_sha"] == "f23000b9771772b6210593d5e611b782e88ba351"
    assert report["github"]["merge_commit_sha"] == "4043ec1bb8db4383dce822ec77d377da44bfee9b"
    assert report["github"]["closing_reference_verified"] is True
    assert report["github"]["pr_discovery"] == "issue_timeline"
    assert report["github"]["outcome"] == "merged"


def test_non_closing_candidate_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    board, profiles = _make_fixture(tmp_path)

    def rest(path: str) -> object:
        if path == f"/repos/{REPOSITORY}/issues/138":
            return {"state": "closed", "full_name": REPOSITORY}
        if path == f"/repos/{REPOSITORY}/issues/138/timeline?per_page=100":
            return [{
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 150,
                        "pull_request": {"url": "x"},
                    }
                },
            }]
        if path == f"/repos/{REPOSITORY}/pulls/150":
            return {
                "number": 150,
                "state": "closed",
                "merged_at": "2026-09-14T00:00:00Z",
                "head": {"sha": "f23000b9771772b6210593d5e611b782e88ba351"},
                "merge_commit_sha": "4043ec1bb8db4383dce822ec77d377da44bfee9b",
                "base": {"repo": {"full_name": REPOSITORY}},
            }
        raise AssertionError(f"unexpected REST path: {path}")

    def graphql(_query: str, _variables: dict[str, object]) -> dict[str, object]:
        return {
            "repository": {
                "pullRequest": {
                    "number": 150,
                    "closingIssuesReferences": {"nodes": [{"number": 71}]},
                },
            },
        }

    monkeypatch.setattr(trajectory, "_default_github_fetch", rest)
    monkeypatch.setattr(trajectory, "_default_github_graphql_fetch", graphql)
    report = trajectory.build_trajectory_report(
        board,
        board_slug="hermes-n8n-control-plane",
        repository=REPOSITORY,
        issue=138,
        task_ids=[],
        profile_root=profiles,
        generated_at=200,
    )
    assert report["github"]["availability"] == "partial"
    assert report["github"]["pr_number"] is None
    assert report["github"]["observed_pr_head_sha"] is None
    assert report["github"]["error"] == "github_pr_closing_reference_unavailable"


def test_partial_usage_buckets_preserve_null_fields_and_coverage(tmp_path: Path) -> None:
    board, profiles = _make_fixture(tmp_path)
    with sqlite3.connect(profiles / "kanban-reviewer" / "state.db") as conn:
        conn.execute("UPDATE sessions SET input_tokens = NULL WHERE id = 'session-review'")
        conn.execute("UPDATE session_model_usage SET output_tokens = NULL WHERE session_id = 'session-review'")
        conn.commit()

    report = trajectory.build_trajectory_report(
        board,
        board_slug="hermes-n8n-control-plane",
        repository=REPOSITORY,
        issue=138,
        profile_root=profiles,
        github_evidence=_github_fixture(),
        generated_at=200,
    )
    profile = report["usage"]["by_profile"]["kanban-reviewer"]
    assert profile["total_runs"] == 1
    assert profile["known_runs"] == 1
    assert profile["input_tokens"] is None
    assert profile["output_tokens"] == 20
    assert profile["total_tokens"] is None
    assert profile["availability"] == "partial"
    assert profile["field_availability"]["input_tokens"] == "unavailable"
    assert profile["coverage"]["input_tokens"] == {"known": 0, "total": 1, "fraction": 0.0}
    assert profile["known_only_totals"]["output_tokens"] == 20

    model = report["usage"]["by_effective_model"]["provider-review:model-review"]
    assert model["known_rows"] == 1
    assert model["input_tokens"] == 10
    assert model["output_tokens"] is None
    assert model["total_tokens"] is None
    assert model["availability"] == "partial"
    assert model["field_availability"]["output_tokens"] == "unavailable"
    assert model["coverage"]["output_tokens"] == {"known": 0, "total": 1, "fraction": 0.0}
    assert model["known_only_totals"]["input_tokens"] == 10

    aggregate = trajectory.aggregate_trajectory_reports([report])
    aggregate_model = aggregate["by_effective_model"]["provider-review:model-review"]
    assert aggregate_model["total_tokens"] is None
    assert aggregate_model["availability"] == "unavailable"


def test_req_154_records_exact_intake_provenance_and_stop_state() -> None:
    req = (MODULE_PATH.parents[3] / ".agent" / "pr-requests" / "REQ-154-trajectory-telemetry.md").read_text()
    assert "Intake idempotency key: github:rhgo1749/hermes-n8n-control-plane:issue:154" in req
    assert "Automation stop state: HUMAN_VALIDATION_REQUIRED" in req
    assert "publishing commit SHA" not in req.casefold()


def test_root_discovery_uses_exact_source_key_and_not_issue_text(tmp_path: Path) -> None:
    board, _profiles = _make_fixture(tmp_path)
    assert trajectory.discover_root_task_ids(board, repository=REPOSITORY) == [("root", 138, 100), ("poison", 154, 120)]
    assert trajectory.discover_root_task_ids(board, repository="other/repository") == []


def test_archived_unrun_canary_is_visible_but_not_a_work_round(tmp_path: Path) -> None:
    board, profiles = _make_fixture(tmp_path)
    conn = sqlite3.connect(board)
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("canary", "archived", "kanban-developer", "kanban-main", 121, None, None, None, "issue138-canary-probe", "canary", "canary"),
    )
    conn.commit()
    conn.close()
    report = trajectory.build_trajectory_report(
        board, board_slug="hermes-n8n-control-plane", repository=REPOSITORY,
        issue=138, profile_root=profiles, github_evidence=_github_fixture(), generated_at=200,
    )
    assert report["counts"]["archived_unrun_canary"] == {"task_id": "canary", "count": 1}
    assert report["counts"]["task_roles"]["developer_total"] == 2
    assert report["counts"]["task_roles"]["developer_work_rounds"] == 1


def test_github_failure_is_partial_and_observer_only(tmp_path: Path) -> None:
    board, profiles = _make_fixture(tmp_path)
    before = hashlib.sha256(board.read_bytes()).hexdigest()

    def unavailable(_path: str) -> dict[str, object]:
        raise OSError("fixture outage")

    report = trajectory.build_trajectory_report(
        board, board_slug="hermes-n8n-control-plane", repository=REPOSITORY,
        issue=138, profile_root=profiles, github_fetcher=unavailable, generated_at=200,
    )
    assert report["status"] == "partial"
    assert report["github"]["availability"] == "unavailable"
    assert report["github"]["issue_closure_authoritative"] is False
    assert report["counts"]["specialist_tasks"] == 4
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before


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
