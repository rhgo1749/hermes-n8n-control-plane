#!/usr/bin/env python3
"""Regression coverage for Issue #92's fail-closed block-kind contract."""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

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


def test_compound_followup_kind_does_not_leak_into_block_segment(board_db):
    """A ``--kind`` from a *later* compound command must not classify the block.

    ``shlex.split`` yields ``&&`` as one token and glues an unspaced ``;`` onto
    the preceding word (``waiting;``), so the argument scan must stop at the
    real shell-control-operator boundary instead of reading the next command's
    options (Issue #92 rework round 4).
    """
    path, _, child = board_db
    commands = (
        f"hermes kanban block {child} waiting && echo --kind=capability",
        f"hermes kanban block {child} waiting; echo --kind=capability",
        f"hermes kanban block {child} waiting&& echo --kind=capability",
        f"hermes kanban block {child} waiting| echo --kind=capability",
        f"bash -lc 'hermes kanban block {child} waiting && echo --kind=capability'",
        f"bash -lc 'hermes kanban block {child} waiting; echo --kind=capability'",
    )
    for command in commands:
        result = _run_guard(
            path,
            {"tool_name": "terminal", "tool_input": {"command": command}},
        )
        _assert_blocked(result, "explicit kind")
        with kanban_db.connect_closing(path) as conn:
            row = conn.execute(
                "SELECT status, block_kind FROM tasks WHERE id = ?", (child,)
            ).fetchone()
        assert tuple(row) == ("ready", None)


def test_explicit_kind_in_block_segment_survives_compound_followup(board_db):
    """A valid block with its own explicit kind still passes when a later
    compound command mentions ``--kind`` (the first segment's kind wins)."""
    path, _, child = board_db
    result = _run_guard(
        path,
        {
            "tool_name": "terminal",
            "tool_input": {
                "command": (
                    f"hermes kanban block {child} waiting --kind=capability"
                    " && echo --kind=dependency"
                )
            },
        },
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_unrelated_followup_compound_remains_fail_open(board_db):
    """A non-block first segment with a later ``--kind`` is unrelated and
    fail-open; the operator boundary must not turn it into a block."""
    path, _, child = board_db
    commands = (
        "echo hi && echo --kind=capability",
        f"hermes kanban show {child} && echo --kind=capability",
        f"hermes kanban unblock {child} && echo --kind=capability",
    )
    for command in commands:
        result = _run_guard(
            path,
            {"tool_name": "terminal", "tool_input": {"command": command}},
        )
        assert result.returncode == 0, (command, result.stdout, result.stderr)


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
        "env -i FOO=bar bash --login -c 'hermes kanban block {child} waiting'",
        "env --ignore-environment --unset=FOO bash -l -c 'hermes kanban block {child} waiting'",
        "command -- bash -cl 'hermes kanban block {child} waiting'",
        "builtin bash -c 'hermes kanban block {child} waiting'",
        "exec -a issue92 bash -lc 'hermes kanban block {child} waiting'",
        "nohup -- bash -c 'hermes kanban block {child} waiting'",
        "env -i command -p bash --login -c 'hermes kanban block {child} waiting'",
        "FOO=bar env -i -u FOO bash -lc 'hermes kanban block {child} waiting'",
    ],
)
def test_launcher_prefixes_and_shell_options_are_unwrapped(board_db, command):
    path, _, child = board_db
    result = _run_guard(
        path,
        {"tool_name": "terminal", "tool_input": {"command": command.format(child=child)}},
    )
    _assert_blocked(result, "explicit kind")


@pytest.mark.parametrize(
    "launcher",
    [
        "env -i FOO=bar",
        "command --",
        "builtin",
        "exec -a issue92",
        "nohup --",
    ],
)
def test_explicit_kind_through_launcher_prefixes_passes(board_db, launcher):
    path, _, child = board_db
    result = _run_guard(
        path,
        {
            "tool_name": "terminal",
            "tool_input": {
                "command": (
                    f"{launcher} bash --login -c "
                    f"'hermes kanban block {child} waiting --kind=capability'"
                )
            },
        },
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_shell_wrapper_depth_limit_fails_closed_without_mutation(board_db):
    path, _, child = board_db
    command = f"hermes kanban block {child} waiting"
    for _ in range(9):
        command = f"bash -lc {shlex.quote(command)}"
    result = _run_guard(
        path,
        {"tool_name": "terminal", "tool_input": {"command": command}},
    )
    _assert_blocked(result, "maximum depth")
    with kanban_db.connect_closing(path) as conn:
        row = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (child,)
        ).fetchone()
    assert tuple(row) == ("ready", None)


@pytest.mark.parametrize(
    "command",
    [
        "printf hello",
        "bash -lc 'printf hello'",
        "sh -c 'ls -la'",
        "bash --rcfile /dev/null -c 'printf hello'",
        "hermes kanban show t_nonexistent",
        "hermes kanban unblock t_nonexistent",
    ],
)
def test_unrelated_commands_fail_open(board_db, command):
    """Commands that do not reference the block family must not be falsely blocked.

    The classifier unwraps only a recognized invocation chain; commands without a
    ``hermes``/``kanban``/``block`` reference (or a different subcommand such as
    ``show``/``unblock``) keep the fail-open behavior rather than being guessed as
    a hidden ``kind``.
    """
    path = board_db[0]
    result = _run_guard(
        path,
        {"tool_name": "terminal", "tool_input": {"command": command}},
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.parametrize(
    "command",
    [
        # A command string that references the block family through an
        # unrecognized shell flag or a non-shell script-form argument cannot be
        # definitively classified, so the gate fails closed instead of allowing
        # the legacy ``kind=None`` mutation path (Issue #92 rework round 3).
        "dash -x 'hermes kanban block t_nonexistent waiting'",
        "/tmp/w.sh 'hermes kanban block t_nonexistent waiting'",
        "eval 'hermes kanban block t_nonexistent waiting'",
    ],
)
def test_family_referencing_unsupported_forms_fail_closed(board_db, command):
    """A family reference the parser cannot definitively unwrap fails closed.

    The conservative execution-family check (rather than an allowlist of exact
    spellings) is what closes the CLI boundary: if the command string could
    execute ``hermes kanban block`` and the gate cannot prove it cannot, it
    blocks instead of guessing a ``kind``.
    """
    path = board_db[0]
    result = _run_guard(
        path,
        {"tool_name": "terminal", "tool_input": {"command": command}},
    )
    assert result.returncode == 2, (result.stdout, result.stderr)


@pytest.mark.parametrize(
    "command",
    [
        # Trusted review findings at head a6d9f84: ordinary valid Bash
        # invocations that actually execute the omitted-kind CLI path.
        "bash -ilc 'hermes kanban block {child} waiting'",
        "bash -e -c 'hermes kanban block {child} waiting'",
        "bash -lc 'eval \"hermes kanban block {child} waiting\"'",
        "bash -lc 'x=hermes; $x kanban block {child} waiting'",
    ],
)
def test_dynamic_and_indirect_block_forms_fail_closed(board_db, command):
    """Dynamic/indirect spellings of the omitted-kind call fail closed.

    A shell option that can reinterpret its argument (``-i``, ``-e``), an
    ``eval`` indirection, or a variable indirection (``x=hermes; $x ...``) all
    actually execute the same ``hermes kanban block`` CLI path, so the gate
    must block them rather than allow the legacy ``kind=None`` mutation.  No
    ``kind`` is inferred or repaired.

    The ``-ilc``/``-e -c`` forms are now definitively unwrapped by the
    inline-command scan (grouped ``c`` plus flag-only ``-i``/``-e``) and blocked
    with a parsed ``explicit kind`` diagnostic; the ``eval``/``$x`` indirection
    forms are not definitively parsed and fail closed on the conservative
    execution-family check.  Either way the gate blocks instead of guessing a
    ``kind``, so the invariant asserted here is: rc=2, a ``block`` action, and
    no task mutation.
    """
    path, _, child = board_db
    result = _run_guard(
        path,
        {"tool_name": "terminal", "tool_input": {"command": command.format(child=child)}},
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    body = json.loads(result.stdout)
    assert body["action"] == "block"
    assert "No task mutation was performed" in body["message"]
    # No task mutation was performed.
    with kanban_db.connect_closing(path) as conn:
        row = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (child,)
        ).fetchone()
    assert tuple(row) == ("ready", None)


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


def test_hook_config_preserves_following_sibling_hook():
    # A valid config where post_tool_call immediately follows pre_tool_call.
    # The pre_tool_call range must terminate at that sibling so the guard is
    # attached to pre_tool_call, not swallowed under post_tool_call.
    original = (
        "hooks:\n"
        "  pre_tool_call:\n"
        "    - matcher: other\n"
        "      command: python3 /tmp/other.py\n"
        "  post_tool_call:\n"
        "    - matcher: audit\n"
        "      command: python3 /tmp/audit.py\n"
        "\n"
        "logging:\n"
        "  level: INFO\n"
    )
    helper = _load("issue92_config_helper", CONFIG_HELPER)
    command = "python3 /home/hermes/.hermes/scripts/kanban-block-kind-guard.py"
    rendered = helper.render(original, command)
    again = helper.render(rendered, command)
    assert rendered == again
    config = yaml.safe_load(rendered)
    pre = config["hooks"]["pre_tool_call"]
    post = config["hooks"]["post_tool_call"]
    pre_matchers = {entry["matcher"] for entry in pre}
    post_matchers = {entry["matcher"] for entry in post}
    assert pre_matchers == {"other", "kanban_block", "terminal"}
    assert post_matchers == {"audit"}
    guard_entries = [
        entry
        for entry in pre
        if "kanban-block-kind-guard.py" in str(entry.get("command", ""))
    ]
    assert {entry["matcher"] for entry in guard_entries} == {"kanban_block", "terminal"}
    assert all(entry.get("fail_closed") is True for entry in guard_entries)
    # the sibling entry and its command survive byte-for-byte
    assert "  post_tool_call:\n" in rendered
    assert "      command: python3 /tmp/audit.py\n" in rendered
    assert "logging:\n" in rendered


def test_history_read_back_preserves_block_kind_across_blocked_runs(board_db):
    # Issue #92 requirement: a PAST status=blocked / outcome=blocked run must not
    # collapse to an undifferentiated "blocked" reading.  Cycle a task through
    # several distinct block kinds (including a legacy None block) and assert the
    # historical read-back preserves each block_kind + dependency provenance.
    path, _, _ = board_db
    with kanban_db.connect_closing(path) as conn:
        # A no-parent task cycles cleanly through ready <-> blocked for the
        # truly-blocked kinds, so each block lands in `blocked` with an
        # outcome=blocked run and a distinct payload.kind.
        task = kanban_db.create_task(conn, title="history-probe", assignee="worker", initial_status="running")
        assert kanban_db.block_task(conn, task, reason="needs maintainer", kind="capability")
        assert kanban_db.unblock_task(conn, task)
        assert kanban_db.block_task(conn, task, reason="waiting on decision", kind="needs_input")
        assert kanban_db.unblock_task(conn, task)
        assert kanban_db.block_task(conn, task, reason="legacy untyped block", kind=None)
        history = sync._task_block_history(conn, task)
        projection = sync._blocked_state_projection(conn, task, None)
        rendered = sync._render_sync_context(
            sync.GithubTaskRef("owner/repo", 92),
            [],
            {},
            block_projection=projection,
            block_history=history,
        )
    # Most-recent-first: legacy untyped, then needs_input, then capability.
    assert [entry["block_kind"] for entry in history] == ["untyped", "needs_input", "capability"]
    # Every truly-blocked entry is dependency-false; none is misclassified as a
    # dependency hold (that is the R2 invariant: omitted/None is not dependency).
    assert all(entry["dependency_driven"] is False for entry in history)
    assert all(entry["auto_promotable"] is False for entry in history)
    # The reason for each historical block is preserved, not erased.
    reasons = {entry["block_kind"]: entry["reason"] for entry in history}
    assert "needs maintainer" in reasons["capability"]
    assert "waiting on decision" in reasons["needs_input"]
    assert "legacy untyped block" in reasons["untyped"]
    # The read-back is rendered into the sync context (a canonical read surface),
    # not only the current Overview row.
    assert "block_history:" in rendered
    assert "block_kind=capability" in rendered
    assert "block_kind=needs_input" in rendered
    assert "block_kind=untyped" in rendered
    assert "auto_promotable=false" in rendered
    assert "dependency_driven=false" in rendered


def test_dependency_block_history_reachable_in_todo_and_after_promotion(board_db):
    # Issue #92 requirement 5 + R6 blocker 1: a canonical kind="dependency"
    # block routes the task to `todo` (auto-promotable), NOT `blocked`.  The
    # dependency-wait history must therefore be exposed by a read surface
    # reachable while the task is in the dependency `todo` path AND after its
    # parent resolves (auto-promotion), not only while it sits in a human
    # `blocked` state.
    path, parent, child = board_db
    with kanban_db.connect_closing(path) as conn:
        # Canonical dependency block on a task with a pending parent.
        assert kanban_db.block_task(conn, child, reason="waiting on parent", kind="dependency")
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id = ?", (child,)).fetchone()
        assert tuple(row) == ("todo", "dependency")

        # While still in the dependency `todo` path (NOT `blocked`), the
        # canonical read surfaces show the dependency hold distinctly.
        history = sync._task_block_history(conn, child)
        assert history and history[0]["kind"] == "dependency_wait"
        assert history[0]["block_kind"] == "dependency"
        assert history[0]["dependency_driven"] is True
        assert history[0]["auto_promotable"] is True
        assert "waiting on parent" in history[0]["reason"]
        rendered = sync._render_sync_context(
            sync.GithubTaskRef("owner/repo", 92), [], {}, block_history=history,
        )
        assert "block_history:" in rendered
        assert "block_kind=dependency" in rendered
        assert "dependency_driven=true" in rendered
        assert "auto_promotable=true" in rendered

        # Resolve the parent -> auto-promote -> the history SURVIVES the
        # promotion (it is status-agnostic, not blocked-only).
        assert kanban_db.complete_task(conn, parent, result="parent done")
        kanban_db.recompute_ready(conn)
        promoted = conn.execute("SELECT status FROM tasks WHERE id = ?", (child,)).fetchone()
        assert promoted[0] == "ready"

        history_after = sync._task_block_history(conn, child)
        assert history_after and history_after[0]["block_kind"] == "dependency"
        assert history_after[0]["dependency_driven"] is True
        assert history_after[0]["auto_promotable"] is True
        rendered_after = sync._render_sync_context(
            sync.GithubTaskRef("owner/repo", 92), [], {}, block_history=history_after,
        )
        assert "block_kind=dependency" in rendered_after
        assert "auto_promotable=true" in rendered_after


def test_dependency_and_human_blocks_stay_distinct_in_history(board_db):
    # A dependency hold and a human-attention hold must never collapse into
    # each other on the historical read surface (Issue #92 invariant).
    path, _, _ = board_db
    with kanban_db.connect_closing(path) as conn:
        task = kanban_db.create_task(conn, title="dep-vs-human", assignee="worker")
        # Parent-less task starts ready; a dependency block routes it to todo.
        assert kanban_db.block_task(conn, task, reason="dependency wait", kind="dependency")
        # Reopen to a blockable state (todo -> ready) so a genuinely different
        # human kind can be applied without guessing a kind.
        kanban_db.recompute_ready(conn)
        assert kanban_db.block_task(conn, task, reason="capability wall", kind="capability")
        history = sync._task_block_history(conn, task)
        # Newest-first: capability (human) then dependency.
        assert [entry["block_kind"] for entry in history][:2] == ["capability", "dependency"]
        by_kind = {entry["block_kind"]: entry for entry in history}
        assert by_kind["dependency"]["dependency_driven"] is True
        assert by_kind["dependency"]["auto_promotable"] is True
        assert by_kind["capability"]["dependency_driven"] is False
        assert by_kind["capability"]["auto_promotable"] is False


def test_history_run_id_binding_prevents_cross_run_summary_leakage(board_db):
    # R6 blocker 2: an event's fallback reason must bind to its OWN run_id,
    # never to an unrelated (newer) blocked run.  Two blocked runs, one older
    # historical event that lacks a payload reason: the older event must fall
    # back to ITS OWN run's summary and must NOT inherit the newer run's
    # summary (the pre-fix query selected the latest blocked run regardless).
    path, _, _ = board_db
    with kanban_db.connect_closing(path) as conn:
        task = kanban_db.create_task(conn, title="run-binding", assignee="worker")
        # Two blocked runs with distinct summaries.
        run1 = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, summary) "
            "VALUES (?, 'worker', 'blocked', 1000, 1001, 'blocked', 'OLD-RUN-SUMMARY')",
            (task,),
        ).lastrowid
        run2 = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, summary) "
            "VALUES (?, 'worker', 'blocked', 2000, 2001, 'blocked', 'NEWER-RUN-SUMMARY')",
            (task,),
        ).lastrowid
        # The OLDER event carries no payload reason -> must fall back to its
        # OWN run (run1) summary.  The NEWER event carries its own reason.
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'dependency_wait', ?, 1001)",
            (task, run1, json.dumps({"kind": "dependency", "source_status": "running"})),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'blocked', ?, 2001)",
            (task, run2, json.dumps({"kind": "capability", "reason": "capability wall"})),
        )
        conn.commit()
        history = sync._task_block_history(conn, task)
        by_kind = {entry["block_kind"]: entry for entry in history}
        # The newer capability entry carries its own (payload) reason.
        assert by_kind["capability"]["reason"] == "capability wall"
        # The older dependency entry falls back to ITS OWN run summary ...
        assert by_kind["dependency"]["reason"] == "OLD-RUN-SUMMARY"
        # ... and never the newer run's summary (no cross-run leakage).
        assert by_kind["dependency"]["reason"] != "NEWER-RUN-SUMMARY"
        assert "NEWER-RUN-SUMMARY" not in by_kind["dependency"]["reason"]
        # The older entry is still a genuine dependency hold.
        assert by_kind["dependency"]["dependency_driven"] is True
        assert by_kind["dependency"]["run_id"] == run1


def test_deployer_dry_run_with_following_sibling_hook():
    with tempfile.TemporaryDirectory(prefix="issue92-hermes-home-sib-") as directory:
        home = Path(directory)
        scripts = home / "scripts"
        scripts.mkdir()
        config = home / "config.yaml"
        config.write_text(
            "hooks:\n"
            "  pre_tool_call:\n"
            "    - matcher: other\n"
            "      command: python3 /tmp/other.py\n"
            "  post_tool_call:\n"
            "    - matcher: audit\n"
            "      command: python3 /tmp/audit.py\n"
            "\n"
            "logging:\n"
            "  level: INFO\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", str(DEPLOYER), "--hermes-home", str(home), "--dry-run"],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "matcher=kanban_block (fail_closed=true)" in result.stdout
        assert "matcher=terminal (fail_closed=true)" in result.stdout
        # dry-run leaves the live config untouched and no candidate behind
        live = config.read_text(encoding="utf-8")
        assert live.count("post_tool_call") == 1
        assert "kanban-block-kind-guard.py" not in live
        assert not any(path.name.startswith(".deploy-candidate-") for path in scripts.iterdir())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
