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
    assert report["counts"]["investigation_search"]["used"] is None
    assert report["investigation_search"]["availability"] == "unavailable"
    assert report["investigation_search"]["candidate_count"] == 0
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
    assert "usage=known" in report["summary"]
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
    assert report["status"] == "partial"
    assert "usage=partial" in report["summary"]
    assert "known-only total_tokens=60" in report["summary"]

    aggregate = trajectory.aggregate_trajectory_reports([report])
    assert aggregate["eligible_denominators"]["token_efficiency"] == 0
    assert aggregate["token_efficiency"] == {
        "total_tokens": None,
        "worker_seconds": None,
        "tokens_per_worker_second": None,
        "availability": "partial",
    }


def _aggregate_report(
    *,
    total_tokens: int | None,
    token_availability: str,
    worker_seconds: int | None,
    worker_availability: str,
) -> dict[str, object]:
    return {
        "identity": {"root_task_row": {"created_at": 1}},
        "counts": {},
        "usage": {
            "totals": {"total_tokens": total_tokens},
            "field_availability": {"total_tokens": token_availability},
            "by_effective_model": {},
        },
        "timing": {
            "summed_worker_seconds": worker_seconds,
            "worker_run_duration_availability": worker_availability,
        },
    }


def test_aggregate_token_efficiency_requires_a_complete_source_report_pair() -> None:
    aggregate = trajectory.aggregate_trajectory_reports([
        _aggregate_report(
            total_tokens=100,
            token_availability="known",
            worker_seconds=None,
            worker_availability="unavailable",
        ),
        _aggregate_report(
            total_tokens=None,
            token_availability="unavailable",
            worker_seconds=10,
            worker_availability="known",
        ),
    ])

    assert aggregate["eligible_denominators"]["token_efficiency"] == 0
    assert aggregate["token_efficiency"] == {
        "total_tokens": None,
        "worker_seconds": None,
        "tokens_per_worker_second": None,
        "availability": "partial",
    }


def test_aggregate_token_efficiency_accepts_one_complete_source_report_pair() -> None:
    aggregate = trajectory.aggregate_trajectory_reports([
        _aggregate_report(
            total_tokens=100,
            token_availability="known",
            worker_seconds=10,
            worker_availability="known",
        ),
    ])

    assert aggregate["eligible_denominators"]["token_efficiency"] == 1
    assert aggregate["token_efficiency"] == {
        "total_tokens": 100,
        "worker_seconds": 10,
        "tokens_per_worker_second": 10.0,
        "availability": "known",
    }


def test_generic_failed_without_cause_is_not_an_infrastructure_retry() -> None:
    counts = trajectory._counts_report(
        {"task": {"status": "done"}},
        [{"id": 1, "task_id": "task", "status": "failed", "outcome": "failed"}],
        [],
        {"task": "developer"},
    )

    assert counts["infrastructure_retries"]["value"] == 0
    assert counts["infrastructure_retries"]["count"] == 0
    assert counts["infrastructure_retries"]["availability"] == "unknown"
    assert counts["infrastructure_retries"]["unknown"] == 1


def test_failed_with_explicit_runtime_cause_is_an_infrastructure_retry() -> None:
    counts = trajectory._counts_report(
        {"task": {"status": "done"}},
        [{
            "id": 1,
            "task_id": "task",
            "status": "failed",
            "outcome": "failed",
            "metadata": json.dumps({"failure_class": "runtime"}),
        }],
        [],
        {"task": "developer"},
    )

    assert counts["infrastructure_retries"]["value"] == 1
    assert counts["infrastructure_retries"]["count"] == 1
    assert counts["infrastructure_retries"]["availability"] == "known"
    assert counts["infrastructure_retries"]["unknown"] == 0


def test_terminal_worker_outcomes_remain_infrastructure_retries() -> None:
    outcomes = ("crashed", "timed_out", "spawn_failed", "reclaimed")
    tasks = {f"task-{index}": {"status": "ready"} for index in range(len(outcomes))}
    runs = [
        {
            "id": index,
            "task_id": f"task-{index}",
            "status": outcome,
            "outcome": outcome,
        }
        for index, outcome in enumerate(outcomes, start=1)
    ]

    counts = trajectory._counts_report(
        tasks,
        runs,
        [],
        {task_id: "developer" for task_id in tasks},
    )

    assert counts["infrastructure_retries"] == {
        "value": 4,
        "count": 4,
        "availability": "known",
        "unknown": 0,
        "definition": "terminal crash/timeout/spawn/reclaim or failed runs with explicit runtime/provider/dispatcher/tool cause evidence only",
    }


def test_failed_with_run_scoped_runtime_event_is_an_infrastructure_retry() -> None:
    counts = trajectory._counts_report(
        {"task": {"status": "done"}},
        [{"id": 1, "task_id": "task", "status": "failed", "outcome": "failed"}],
        [{"task_id": "task", "run_id": 1, "kind": "runtime_failure", "payload": "{}"}],
        {"task": "developer"},
    )

    assert counts["infrastructure_retries"]["value"] == 1
    assert counts["infrastructure_retries"]["availability"] == "known"

    unrelated_counts = trajectory._counts_report(
        {"task": {"status": "done"}},
        [{"id": 1, "task_id": "task", "status": "failed", "outcome": "failed"}],
        [{"task_id": "task", "run_id": 2, "kind": "runtime_failure", "payload": "{}"}],
        {"task": "developer"},
    )

    assert unrelated_counts["infrastructure_retries"]["value"] == 0
    assert unrelated_counts["infrastructure_retries"]["availability"] == "unknown"


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


def _search_run(
    run_id: int,
    task_id: str,
    phase: str,
    *,
    candidate_id: str | None = None,
    started_at: int = 10,
    ended_at: int = 20,
    **marker_fields: object,
) -> dict[str, object]:
    marker: dict[str, object] = {
        "schema_id": trajectory.INVESTIGATION_SEARCH_SCHEMA_ID,
        "search_id": "search-155",
        "phase": phase,
        "trigger_codes": ["RUNTIME_TEST_CONTRADICTION"],
        "budget": {
            "max_candidates": 2,
            "max_expansions": 1,
            "max_runtime_seconds": 900,
            "max_retries": 1,
            "max_total_tokens": 32000,
        },
        **marker_fields,
    }
    if candidate_id is not None:
        marker["candidate_id"] = candidate_id
    return {
        "id": run_id,
        "task_id": task_id,
        "profile": "kanban-investigator" if phase == "candidate" else "kanban-main",
        "status": "done",
        "outcome": "completed",
        "started_at": started_at,
        "ended_at": ended_at,
        "metadata": json.dumps({
            "worker_session_id": f"session-{task_id}",
            "investigation_search": marker,
        }),
    }


def test_explicit_investigation_search_projects_candidates_selection_and_cost() -> None:
    runs = [
        _search_run(
            11, "candidate-a", "candidate", candidate_id="A", ended_at=20,
            candidate_task_id="candidate-a", closure_status="sufficient",
            confidence="HIGH", independence_basis="independent-boundary-a",
        ),
        _search_run(
            12, "candidate-b", "candidate", candidate_id="B", started_at=12,
            ended_at=32, candidate_task_id="candidate-b", closure_status="sufficient",
            confidence="MEDIUM", independence_basis="independent-boundary-b",
        ),
        _search_run(
            13, "selector", "selector", started_at=33, ended_at=35,
            candidate_task_ids=["candidate-a", "candidate-b"],
            selected_candidate_task_id="candidate-b", selection_status="selected",
            selection_reason_code="clearer_falsification",
        ),
    ]
    usage = [
        {"run_id": 11, "session": {"input_tokens": 10, "output_tokens": 20}},
        {"run_id": 12, "session": {"input_tokens": 30, "output_tokens": 40}},
    ]

    report = trajectory._investigation_search_report(runs, [], usage)

    assert report["availability"] == "known"
    assert report["used"] is True
    assert report["search_count"] == 1
    assert report["candidate_count"] == 2
    search = report["searches"][0]
    assert search["candidate_task_ids"] == ["candidate-a", "candidate-b"]
    assert search["selected_candidate_task_ids"] == ["candidate-b"]
    assert search["selection_status"] == "selected"
    assert search["cost"] == {
        "total_tokens": 100,
        "known_only_total_tokens": 100,
        "token_availability": "known",
        "worker_seconds": 30,
        "known_only_worker_seconds": 30,
        "worker_seconds_availability": "known",
        "monetary": {
            "value": None,
            "availability": "unavailable",
            "reason": "no_authoritative_per_task_charge",
        },
    }
    assert report["cost"]["total_tokens"] == 100
    assert report["outcomes"] == {"selected": 1}


def test_search_projection_keeps_missing_marker_and_model_refresh_distinct() -> None:
    refresh = {
        "id": 14,
        "task_id": "refresh",
        "profile": "kanban-investigator",
        "status": "done",
        "outcome": "completed",
        "metadata": json.dumps({
            "worker_session_id": "session-refresh",
            "investigation_model_refresh": True,
        }),
    }

    report = trajectory._investigation_search_report([refresh], [], [])

    assert report["availability"] == "unavailable"
    assert report["used"] is None
    assert report["search_count"] == 0
    assert report["candidate_count"] == 0


def test_malformed_candidate_marker_is_not_counted_as_search_work() -> None:
    malformed = {
        "id": 15,
        "task_id": "candidate-c",
        "profile": "kanban-investigator",
        "status": "done",
        "outcome": "completed",
        "metadata": json.dumps({
            "investigation_search": {
                "schema_id": trajectory.INVESTIGATION_SEARCH_SCHEMA_ID,
                "search_id": "search-155",
                "phase": "candidate",
                "candidate_id": "C",
            },
        }),
    }

    report = trajectory._investigation_search_report([malformed], [], [])

    assert report["availability"] == "unavailable"
    assert report["candidate_count"] == 0


def test_partial_candidate_usage_is_null_total_not_zero() -> None:
    runs = [
        _search_run(
            21, "candidate-a", "candidate", candidate_id="A",
            candidate_task_id="candidate-a", closure_status="sufficient",
        ),
        _search_run(
            22, "candidate-b", "candidate", candidate_id="B",
            candidate_task_id="candidate-b", closure_status="sufficient",
        ),
    ]
    usage = [
        {"run_id": 21, "session": {"input_tokens": 10, "output_tokens": 20}},
        {"run_id": 22, "session": {"input_tokens": 30, "output_tokens": None}},
    ]

    report = trajectory._investigation_search_report(runs, [], usage)
    cost = report["searches"][0]["cost"]

    assert cost["total_tokens"] is None
    assert cost["known_only_total_tokens"] == 60
    assert cost["token_availability"] == "partial"


def test_reviewer_rework_classification_is_explicit_and_not_inferred() -> None:
    marker = {
        "schema_id": trajectory.INVESTIGATION_SEARCH_SCHEMA_ID,
        "search_id": "search-155",
        "phase": "selector",
        "rework_class": "implementation_gap",
    }
    counts = trajectory._counts_report(
        {"review": {"status": "done"}},
        [{
            "id": 25,
            "task_id": "review",
            "profile": "kanban-reviewer",
            "status": "done",
            "outcome": "completed",
            "metadata": json.dumps({"verdict": "REWORK", "investigation_search": marker}),
        }],
        [],
        {"review": "reviewer"},
    )
    unmarked = trajectory._counts_report(
        {"review": {"status": "done"}},
        [{
            "id": 26,
            "task_id": "review",
            "profile": "kanban-reviewer",
            "status": "done",
            "outcome": "completed",
            "metadata": json.dumps({"verdict": "REWORK"}),
        }],
        [],
        {"review": "reviewer"},
    )

    assert counts["reviewer_rework_classification"] == {
        "implementation_gap": 1,
        "investigation_model_refresh": 0,
        "UNKNOWN": 0,
        "classified_rework": 1,
        "availability": "known",
    }
    assert unmarked["reviewer_rework_classification"]["implementation_gap"] == 0
    assert unmarked["reviewer_rework_classification"]["UNKNOWN"] == 1


def test_aggregate_investigation_search_preserves_candidate_denominators() -> None:
    search = trajectory._investigation_search_report(
        [
            _search_run(
                31, "candidate-a", "candidate", candidate_id="A",
                candidate_task_id="candidate-a", closure_status="sufficient",
            ),
            _search_run(
                32, "candidate-b", "candidate", candidate_id="B",
                candidate_task_id="candidate-b", closure_status="sufficient",
            ),
            _search_run(
                33, "selector", "selector", candidate_task_ids=["candidate-a", "candidate-b"],
                selected_candidate_task_id="candidate-a", selection_status="selected",
            ),
        ],
        [],
        [
            {"run_id": 31, "session": {"input_tokens": 1, "output_tokens": 2}},
            {"run_id": 32, "session": {"input_tokens": 3, "output_tokens": 4}},
        ],
    )

    aggregate = trajectory.aggregate_trajectory_reports([{"investigation_search": search}])

    assert aggregate["investigation_search"]["search_count"] == 1
    assert aggregate["investigation_search"]["candidate_count"] == 2
    assert aggregate["investigation_search"]["selected_candidate_task_ids"] == ["candidate-a"]
    assert aggregate["investigation_search"]["cost"]["total_tokens"] == 10


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
    conn.execute("INSERT INTO task_links VALUES (?, ?)", ("root", "canary"))
    conn.commit()
    conn.close()
    report = trajectory.build_trajectory_report(
        board, board_slug="hermes-n8n-control-plane", repository=REPOSITORY,
        issue=138, profile_root=profiles, github_evidence=_github_fixture(), generated_at=200,
    )
    assert report["counts"]["archived_unrun_canary"] == {"task_id": "canary", "count": 1}
    assert report["counts"]["task_roles"]["developer_total"] == 2
    assert report["counts"]["task_roles"]["developer_work_rounds"] == 1


def test_canary_scope_is_repository_qualified_or_root_linked() -> None:
    tasks = {
        "root_a": {"id": "root_a", "idempotency_key": "github:ownerA/repoA:issue:138"},
        "root_b": {"id": "root_b", "idempotency_key": "github:ownerB/repoB:issue:138"},
        "qualified_a": {
            "id": "qualified_a",
            "idempotency_key": "github:ownerA/repoA:issue:138:canary:qualified",
        },
        "qualified_b": {
            "id": "qualified_b",
            "idempotency_key": "github:ownerB/repoB:issue:138:canary:qualified",
        },
        "linked_a": {"id": "linked_a", "idempotency_key": "issue138-canary-linked-a"},
        "linked_b": {"id": "linked_b", "idempotency_key": "issue138-canary-linked-b"},
        "unlinked": {"id": "unlinked", "idempotency_key": "issue138-canary-unlinked"},
        "verified_legacy": {
            "id": "verified_legacy",
            "idempotency_key": "issue138-canary-" + ("1" * 40),
        },
    }
    _root_id, _root_status, _root, selected, _provenance = trajectory._select_scope(
        tasks,
        [
            {"parent_id": "root_a", "child_id": "linked_a"},
            {"parent_id": "root_b", "child_id": "linked_b"},
        ],
        repository="ownerA/repoA",
        issue=138,
        root_task_id=None,
        task_ids=None,
    )

    assert set(selected) == {"root_a", "qualified_a", "linked_a"}

    _root_id, _root_status, _root, selected, _provenance = trajectory._select_scope(
        tasks,
        [
            {"parent_id": "root_a", "child_id": "linked_a"},
            {"parent_id": "root_b", "child_id": "linked_b"},
        ],
        repository="ownerA/repoA",
        issue=138,
        root_task_id=None,
        task_ids=None,
        verified_canary_ids=("1" * 40,),
    )
    assert set(selected) == {"root_a", "qualified_a", "linked_a", "verified_legacy"}


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


def test_github_outcome_prefers_sole_merged_closing_candidate() -> None:
    issue_data = {"state": "closed", "full_name": REPOSITORY}
    pull_requests = {
        149: {
            "number": 149,
            "state": "closed",
            "merged_at": None,
            "head": {"sha": "1111111111111111111111111111111111111111"},
            "base": {"repo": {"full_name": REPOSITORY}},
        },
        150: {
            "number": 150,
            "state": "closed",
            "merged_at": "2026-09-14T01:00:00Z",
            "head": {"sha": "2222222222222222222222222222222222222222"},
            "merge_commit_sha": "3333333333333333333333333333333333333333",
            "base": {"repo": {"full_name": REPOSITORY}},
        },
    }

    def rest(path: str) -> object:
        if path == f"/repos/{REPOSITORY}/issues/138":
            return issue_data
        if path == f"/repos/{REPOSITORY}/issues/138/timeline?per_page=100":
            return [
                {"event": "cross-referenced", "source": {"issue": {"number": 149, "pull_request": {"url": "x"}}}},
                {"event": "cross-referenced", "source": {"issue": {"number": 150, "pull_request": {"url": "x"}}}},
            ]
        if path in {f"/repos/{REPOSITORY}/pulls/149", f"/repos/{REPOSITORY}/pulls/150"}:
            return pull_requests[int(path.rsplit("/", 1)[1])]
        raise AssertionError(f"unexpected REST path: {path}")

    def graphql(_query: str, variables: dict[str, object]) -> dict[str, object]:
        number = int(str(variables["number"]))
        return {
            "repository": {
                "pullRequest": {
                    "number": number,
                    "repository": {"nameWithOwner": REPOSITORY},
                    "closingIssuesReferences": {
                        "nodes": [{"number": 138, "repository": {"nameWithOwner": REPOSITORY}}],
                    },
                },
            },
        }

    result = trajectory._github_outcome(
        REPOSITORY,
        138,
        [],
        github_evidence=None,
        github_fetcher=rest,
        github_graphql_fetcher=graphql,
        generated_at=200,
    )

    assert result["pr_number"] == 150
    assert result["outcome"] == "merged"
    assert result["observed_pr_head_sha"] == "2222222222222222222222222222222222222222"
    assert result["merge_commit_sha"] == "3333333333333333333333333333333333333333"
    assert result["closing_reference_verified"] is True


def test_github_outcome_fails_closed_for_ambiguous_unmerged_closing_candidates() -> None:
    issue_data = {"state": "closed", "full_name": REPOSITORY}

    def rest(path: str) -> object:
        if path == f"/repos/{REPOSITORY}/issues/138":
            return issue_data
        if path == f"/repos/{REPOSITORY}/issues/138/timeline?per_page=100":
            return [
                {"event": "cross-referenced", "source": {"issue": {"number": 149, "pull_request": {"url": "x"}}}},
                {"event": "cross-referenced", "source": {"issue": {"number": 150, "pull_request": {"url": "x"}}}},
            ]
        if path in {f"/repos/{REPOSITORY}/pulls/149", f"/repos/{REPOSITORY}/pulls/150"}:
            number = int(path.rsplit("/", 1)[1])
            return {
                "number": number,
                "state": "closed",
                "merged_at": None,
                "base": {"repo": {"full_name": REPOSITORY}},
            }
        raise AssertionError(f"unexpected REST path: {path}")

    def graphql(_query: str, variables: dict[str, object]) -> dict[str, object]:
        return {
            "repository": {
                "pullRequest": {
                    "number": int(str(variables["number"])),
                    "repository": {"nameWithOwner": REPOSITORY},
                    "closingIssuesReferences": {
                        "nodes": [{"number": 138, "repository": {"nameWithOwner": REPOSITORY}}],
                    },
                },
            },
        }

    result = trajectory._github_outcome(
        REPOSITORY,
        138,
        [],
        github_evidence=None,
        github_fetcher=rest,
        github_graphql_fetcher=graphql,
        generated_at=200,
    )

    assert result["availability"] == "partial"
    assert result["pr_number"] is None
    assert result["error"] == "github_pr_closing_reference_ambiguous"


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
