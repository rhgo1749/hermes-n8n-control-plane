#!/usr/bin/env python3
"""Regression coverage for Issue #92's fail-closed block-kind contract."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, "/ws/hermes-agent")
from hermes_cli import kanban_db  # type: ignore[import-not-found]

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "automation/hermes/scripts/kanban-block-kind-guard.py"
CONFIG_HELPER = REPO_ROOT / "automation/hermes/scripts/kanban-block-kind-hook-config.py"
DEPLOYER = REPO_ROOT / "automation/hermes/scripts/deploy-intake-edge.sh"
SYNC_PATH = REPO_ROOT / "edge/kanban-github-sync.py"
OVERVIEW_PATH = REPO_ROOT / "hermes-plugin/h4v3-overview/dashboard/plugin_api.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync = _load("issue92_sync", SYNC_PATH)
overview = _load("issue92_overview", OVERVIEW_PATH)


@pytest.fixture
def board_db() -> Iterator[tuple[Path, str, str]]:
    with tempfile.TemporaryDirectory(prefix="issue92-board-") as directory:
        path = Path(directory) / "kanban.db"
        kanban_db.init_db(path)
        with kanban_db.connect_closing(path) as conn:
            parent = kanban_db.create_task(
                conn, title="parent", assignee="worker", initial_status="running"
            )
            child = kanban_db.create_task(
                conn, title="child", assignee="worker", initial_status="running"
            )
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent, child),
            )
            conn.commit()
        yield path, parent, child


def _run_guard(path: Path, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="issue92-guard-log-") as log_dir:
        environment = os.environ.copy()
        environment.update(
            {
                "HERMES_KANBAN_DB": str(path),
                "KANBAN_BLOCK_KIND_GUARD_LOG": str(Path(log_dir) / "guard.log"),
            }
        )
        return subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )


def _assert_blocked(result: subprocess.CompletedProcess[str], text: str) -> None:
    assert result.returncode == 2, result.stderr
    body = json.loads(result.stdout)
    assert body["action"] == "block"
    assert text in body["message"]
    assert "No task mutation was performed" in body["message"]


def test_missing_kind_with_pending_parent_fails_closed_without_mutation(board_db):
    path, parent, child = board_db
    with kanban_db.connect_closing(path) as conn:
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    result = _run_guard(
        path,
        {"tool_name": "kanban_block", "tool_input": {"task_id": child}},
    )
    _assert_blocked(result, f"{parent}(ready)")
    with kanban_db.connect_closing(path) as conn:
        row = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (child,)
        ).fetchone()
        after_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    assert tuple(row) == ("ready", None)
    assert after_events == before_events


def test_pre_fix_bite_proof_core_accepts_legacy_omitted_kind(board_db):
    """The guard regression is meaningful because core still accepts None."""
    path, _, child = board_db
    with kanban_db.connect_closing(path) as conn:
        assert kanban_db.block_task(conn, child, reason="legacy caller", kind=None)
        row = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (child,)
        ).fetchone()
    assert tuple(row) == ("blocked", None)


def test_missing_kind_without_pending_parent_requests_explicit_choice(board_db):
    path, _, child = board_db
    with kanban_db.connect_closing(path) as conn:
        kanban_db.block_task(conn, child, reason="operator decision", kind="needs_input")
        before = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?", (child,)
        ).fetchone()
    # Remove the only parent after the durable blocked row exists; this keeps
    # the fixture's schema real while exercising the no-pending diagnostic.
    with kanban_db.connect_closing(path) as conn:
        conn.execute("DELETE FROM task_links WHERE child_id = ?", (child,))
        conn.commit()
    result = _run_guard(
        path,
        {"tool_name": "kanban_block", "tool_input": {"task_id": child, "kind": ""}},
    )
    _assert_blocked(result, "pick an explicit kind")
    with kanban_db.connect_closing(path) as conn:
        after = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?", (child,)
        ).fetchone()
    assert tuple(after) == tuple(before)


@pytest.mark.parametrize("kind", ["dependency", "needs_input", "capability", "transient"])
def test_explicit_canonical_kind_passes_after_read_only_validation(board_db, kind):
    path, _, child = board_db
    result = _run_guard(
        path,
        {"tool_name": "kanban_block", "tool_input": {"task_id": child, "kind": kind}},
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == ""


def test_explicit_dependency_preserves_core_todo_then_ready_route(board_db):
    path, parent, child = board_db
    result = _run_guard(
        path,
        {"tool_name": "kanban_block", "tool_input": {"task_id": child, "kind": "dependency"}},
    )
    assert result.returncode == 0
    with kanban_db.connect_closing(path) as conn:
        assert kanban_db.block_task(conn, child, reason="wait for parent", kind="dependency")
        waiting = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (child,)
        ).fetchone()
        assert tuple(waiting) == ("todo", "dependency")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.commit()
        assert kanban_db.recompute_ready(conn) == 1
        ready = conn.execute("SELECT status FROM tasks WHERE id = ?", (child,)).fetchone()
    assert tuple(ready) == ("ready",)


def test_explicit_human_kind_is_distinct_from_dependency_wait(board_db):
    path, _, child = board_db
    result = _run_guard(
        path,
        {"tool_name": "kanban_block", "tool_input": {"task_id": child, "kind": "needs_input"}},
    )
    assert result.returncode == 0
    with kanban_db.connect_closing(path) as conn:
        assert kanban_db.block_task(conn, child, reason="needs decision", kind="needs_input")
        row = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (child,)
        ).fetchone()
    assert tuple(row) == ("blocked", "needs_input")


@pytest.mark.parametrize("kind", [None, "", "unknown", 3, []])
def test_none_empty_malformed_and_unknown_kinds_are_rejected(board_db, kind):
    path, _, child = board_db
    result = _run_guard(
        path,
        {"tool_name": "kanban_block", "tool_input": {"task_id": child, "kind": kind}},
    )
    _assert_blocked(result, "kind")


def test_unresolvable_board_fails_closed_even_for_explicit_kind():
    result = _run_guard(
        Path("/tmp/issue92-board-does-not-exist.db"),
        {"tool_name": "kanban_block", "tool_input": {"task_id": "t_missing", "kind": "dependency"}},
    )
    _assert_blocked(result, "failed closed")


def test_terminal_matcher_only_intercepts_hermes_block(board_db):
    path, _, child = board_db
    unrelated = _run_guard(path, {"tool_name": "terminal", "tool_input": {"command": "printf hello"}})
    assert unrelated.returncode == 0
    missing = _run_guard(
        path,
        {
            "tool_name": "terminal",
            "tool_input": {"command": f"hermes kanban block {child} waiting"},
        },
    )
    _assert_blocked(missing, "explicit kind")
    explicit = _run_guard(
        path,
        {
            "tool_name": "terminal",
            "tool_input": {"command": f"hermes kanban block {child} waiting --kind=capability"},
        },
    )
    assert explicit.returncode == 0


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'hermes kanban block {child} waiting'",
        "sh -c 'hermes kanban block {child} waiting'",
        "bash -c \"sh -c 'hermes kanban block {child} waiting'\"",
        "bash -lc 'git status && hermes kanban block {child} waiting'",
        "HERMES_KANBAN_BOARD=x bash -lc 'hermes kanban block {child} waiting'",
    ],
)
def test_omitted_kind_through_shell_wrappers_is_blocked(board_db, command):
    """A shell wrapper must not bypass the fail-closed gate (Issue #92 rework).

    ``hermes kanban block`` hidden behind a supported ``sh``/``bash`` ``-c``/``-lc``
    wrapper is unwrapped and classified, so an omitted ``kind`` still fails closed
    instead of reaching the legacy ``kind=None`` durable block path.
    """
    path, _, child = board_db
    result = _run_guard(
        path,
        {"tool_name": "terminal", "tool_input": {"command": command.format(child=child)}},
    )
    _assert_blocked(result, "explicit kind")


def test_explicit_kind_through_shell_wrapper_passes(board_db):
    path, _, child = board_db
    result = _run_guard(
        path,
        {
            "tool_name": "terminal",
            "tool_input": {
                "command": f"bash -lc 'hermes kanban block {child} waiting --kind=capability'"
            },
        },
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'printf hello'",
        "sh -c 'ls -la'",
        "dash -x 'hermes kanban block t_nonexistent waiting'",
        "/tmp/w.sh 'hermes kanban block t_nonexistent waiting'",
    ],
)
def test_unrelated_or_unsupported_wrappers_fail_open(board_db, command):
    """Wrappers the gate does not recognize must not be falsely blocked.

    ``sh``/``bash`` ``-c``/``-lc`` unwrapping is intentionally narrow; an unknown
    binary, a bare script path, or an unrecognized flag keeps the previous fail-open
    behavior rather than guessing at a hidden ``kind``.
    """
    path = board_db[0]
    result = _run_guard(
        path,
        {"tool_name": "terminal", "tool_input": {"command": command}},
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_block_projection_and_sync_context_keep_kind_and_parent_provenance(board_db):
    path, parent, child = board_db
    with kanban_db.connect_closing(path) as conn:
        kanban_db.block_task(conn, child, reason="needs maintainer", kind="capability")
        projection = sync._blocked_state_projection(conn, child, "capability")
    assert projection["block_kind"] == "capability"
    assert projection["dependency_driven"] is False
    assert projection["auto_promotable"] is False
    assert projection["pending_parent_ids"] == [parent]
    rendered = sync._render_sync_context(
        sync.GithubTaskRef("owner/repo", 92), [], {}, block_projection=projection
    )
    assert "block:" in rendered
    assert "block_kind: capability" in rendered
    assert f'pending_parent_ids: ["{parent}"]' in rendered


def test_overview_projects_blocked_task_as_untyped_when_legacy_kind_is_null(board_db):
    path, _, child = board_db
    with kanban_db.connect_closing(path) as conn:
        kanban_db.block_task(conn, child, reason="legacy", kind=None)
    metadata = {"slug": "issue92", "db_path": str(path), "name": "Issue 92"}
    board = overview._load_board_projection(metadata)
    task = next(item for item in board["tasks"] if item["id"] == child)
    assert task["status"] == "blocked"
    assert task["block_kind"] == "untyped"
    assert task["block"]["block_kind"] == "untyped"
    assert task["block"]["auto_promotable"] is False


def test_hook_config_render_is_idempotent_and_fail_closed():
    original = """hooks:\n  pre_tool_call:\n    - matcher: other\n      command: python3 /tmp/other.py\n      timeout: 5\n\nlogging:\n  level: INFO\n"""
    helper = _load("issue92_config_helper", CONFIG_HELPER)
    command = "python3 /home/hermes/.hermes/scripts/kanban-block-kind-guard.py"
    rendered = helper.render(original, command)
    again = helper.render(rendered, command)
    assert rendered == again
    assert rendered.count("matcher: kanban_block") == 1
    assert rendered.count("matcher: terminal") == 1
    assert rendered.count("fail_closed: true") == 2
    assert "command: python3 /home/hermes/.hermes/scripts/kanban-block-kind-guard.py" in rendered
    assert "logging:\n" in rendered


def test_deployer_dry_run_validates_without_changing_live_config():
    with tempfile.TemporaryDirectory(prefix="issue92-hermes-home-") as directory:
        home = Path(directory)
        scripts = home / "scripts"
        scripts.mkdir()
        config = home / "config.yaml"
        original = "hooks:\n  pre_tool_call:\n    - matcher: other\n      command: python3 /tmp/other.py\n"
        config.write_text(original, encoding="utf-8")
        result = subprocess.run(
            ["bash", str(DEPLOYER), "--hermes-home", str(home), "--dry-run"],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "matcher=kanban_block (fail_closed=true)" in result.stdout
        assert "matcher=terminal (fail_closed=true)" in result.stdout
        assert config.read_text(encoding="utf-8") == original
        assert not any(path.name.startswith(".deploy-candidate-") for path in scripts.iterdir())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
