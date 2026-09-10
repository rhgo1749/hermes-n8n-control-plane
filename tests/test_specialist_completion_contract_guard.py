#!/usr/bin/env python3
"""Regression coverage for the H4V3 specialist completion-contract boundary."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "automation/hermes/scripts/kanban-block-kind-guard.py"
CONFIG_HELPER = ROOT / "automation/hermes/scripts/kanban-block-kind-hook-config.py"
DEPLOYER = ROOT / "automation/hermes/scripts/deploy-intake-edge.sh"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(
    payload: dict[str, Any],
    *,
    env_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="specialist-contract-guard-") as directory:
        env = os.environ.copy()
        env["KANBAN_SPECIALIST_COMPLETION_GUARD_LOG"] = str(Path(directory) / "guard.log")
        env.update(env_updates or {})
        return subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )


def _task_db(path: Path, task_id: str, contract: str | None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, completion_contract TEXT)")
        conn.execute(
            "INSERT INTO tasks (id, completion_contract) VALUES (?, ?)",
            (task_id, contract),
        )
        conn.commit()
    finally:
        conn.close()


def _assert_blocked(result: subprocess.CompletedProcess[str], assignee: str) -> None:
    assert result.returncode == 2, (result.stdout, result.stderr)
    body = json.loads(result.stdout)
    assert body["action"] == "block"
    assert assignee in body["message"]
    assert "completion_contract=local-only" in body["message"]
    assert "root-card edge reconciliation" in body["message"]
    assert "No task mutation was performed" in body["message"]


def test_structured_specialists_reject_repository_contract() -> None:
    for assignee in ("kanban-developer", "kanban-reviewer", "kanban-designer"):
        result = _run(
            {
                "tool_name": "kanban_create",
                "tool_input": {
                    "title": "bounded specialist work",
                    "assignee": assignee,
                    "completion_contract": "rhgo1749/ctrl-hangul",
                },
            }
        )
        _assert_blocked(result, assignee)


def test_structured_specialist_rejects_exact_pr_contract() -> None:
    result = _run(
        {
            "tool_name": "kanban_create",
            "tool_input": {
                "title": "R10 rework",
                "assignee": "kanban-developer",
                "completion_contract": "https://github.com/rhgo1749/ctrl-hangul/pull/111",
            },
        }
    )
    _assert_blocked(result, "kanban-developer")


def test_structured_specialist_allows_omitted_and_local_only_contract() -> None:
    for value in (None, "local-only"):
        tool_input: dict[str, Any] = {
            "title": "bounded specialist work",
            "assignee": "kanban-developer",
        }
        if value is not None:
            tool_input["completion_contract"] = value
        result = _run({"tool_name": "kanban_create", "tool_input": tool_input})
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.stdout == ""


def test_non_specialist_pr_contract_is_not_globally_disabled() -> None:
    result = _run(
        {
            "tool_name": "kanban_create",
            "tool_input": {
                "title": "standalone PR acceptance task",
                "assignee": "kanban-main",
                "completion_contract": "rhgo1749/ctrl-hangul",
            },
        }
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == ""


def test_structured_specialist_malformed_nonlocal_contract_fails_closed() -> None:
    result = _run(
        {
            "tool_name": "kanban_create",
            "tool_input": {
                "title": "bounded specialist work",
                "assignee": "kanban-reviewer",
                "completion_contract": {"unexpected": "object"},
            },
        }
    )
    _assert_blocked(result, "kanban-reviewer")


def test_terminal_literal_and_shell_wrapped_nonlocal_contracts_are_blocked() -> None:
    commands = (
        "hermes kanban create x --assignee kanban-developer --completion-contract rhgo1749/ctrl-hangul",
        "bash -lc 'hermes kanban create x --assignee kanban-reviewer --completion-contract https://github.com/rhgo1749/ctrl-hangul/pull/111'",
        "env FOO=1 sh -c 'hermes kanban create x --assignee=kanban-designer --completion-contract=rhgo1749/ctrl-hangul'",
    )
    expected = ("kanban-developer", "kanban-reviewer", "kanban-designer")
    for command, assignee in zip(commands, expected, strict=True):
        result = _run({"tool_name": "terminal", "tool_input": {"command": command}})
        _assert_blocked(result, assignee)


def test_terminal_specialist_local_only_or_omitted_contract_is_allowed() -> None:
    commands = (
        "hermes kanban create x --assignee kanban-developer --completion-contract local-only",
        "bash -lc 'hermes kanban create x --assignee=kanban-reviewer --completion-contract=local-only'",
        "hermes kanban create x --assignee kanban-designer",
    )
    for command in commands:
        result = _run({"tool_name": "terminal", "tool_input": {"command": command}})
        assert result.returncode == 0, (command, result.stdout, result.stderr)


def test_terminal_mixed_contract_values_cannot_hide_nonlocal_create() -> None:
    command = (
        "hermes kanban create x --assignee kanban-developer "
        "--completion-contract rhgo1749/ctrl-hangul && "
        "printf '%s' '--completion-contract local-only'"
    )
    result = _run({"tool_name": "terminal", "tool_input": {"command": command}})
    _assert_blocked(result, "kanban-developer")


def test_terminal_assignment_to_specialist_rejects_existing_pr_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="specialist-contract-assign-") as directory:
        db = Path(directory) / "kanban.db"
        _task_db(db, "t_praware", "https://github.com/rhgo1749/ctrl-hangul/pull/111")
        result = _run(
            {
                "tool_name": "terminal",
                "tool_input": {"command": "hermes kanban assign t_praware kanban-developer"},
            },
            env_updates={"HERMES_KANBAN_DB": str(db)},
        )
    _assert_blocked(result, "kanban-developer")


def test_terminal_reassign_to_specialist_rejects_repository_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="specialist-contract-reassign-") as directory:
        db = Path(directory) / "kanban.db"
        _task_db(db, "t_repoaware", "rhgo1749/ctrl-hangul")
        result = _run(
            {
                "tool_name": "terminal",
                "tool_input": {
                    "command": "bash -lc 'hermes kanban reassign t_repoaware kanban-reviewer --reclaim'"
                },
            },
            env_updates={"HERMES_KANBAN_DB": str(db)},
        )
    _assert_blocked(result, "kanban-reviewer")


def test_terminal_assignment_to_specialist_allows_local_only_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="specialist-contract-local-assign-") as directory:
        db = Path(directory) / "kanban.db"
        _task_db(db, "t_local", "local-only")
        result = _run(
            {
                "tool_name": "terminal",
                "tool_input": {"command": "hermes kanban assign t_local kanban-designer"},
            },
            env_updates={"HERMES_KANBAN_DB": str(db)},
        )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_terminal_assignment_uses_explicit_board_read_only() -> None:
    with tempfile.TemporaryDirectory(prefix="specialist-contract-board-assign-") as directory:
        home = Path(directory) / ".hermes"
        db = home / "kanban" / "boards" / "ctrl-hangul" / "kanban.db"
        db.parent.mkdir(parents=True)
        _task_db(db, "t_board", "rhgo1749/ctrl-hangul")
        result = _run(
            {
                "tool_name": "terminal",
                "tool_input": {
                    "command": "hermes kanban --board ctrl-hangul assign t_board kanban-developer"
                },
            },
            env_updates={"HERMES_HOME": str(home), "HERMES_KANBAN_DB": ""},
        )
    _assert_blocked(result, "kanban-developer")


def test_terminal_assignment_with_unreadable_task_state_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="specialist-contract-missing-assign-") as directory:
        db = Path(directory) / "kanban.db"
        _task_db(db, "t_other", "local-only")
        result = _run(
            {
                "tool_name": "terminal",
                "tool_input": {"command": "hermes kanban assign t_missing kanban-developer"},
            },
            env_updates={"HERMES_KANBAN_DB": str(db)},
        )
    assert result.returncode == 2, (result.stdout, result.stderr)
    body = json.loads(result.stdout)
    assert body["action"] == "block"
    assert "failed closed" in body["message"]
    assert "t_missing" in body["message"]
    assert "No task mutation was performed" in body["message"]


def test_terminal_profile_text_without_specialist_assignee_is_not_a_false_positive() -> None:
    command = (
        "hermes kanban create 'document kanban-developer behavior' "
        "--assignee kanban-main --completion-contract rhgo1749/ctrl-hangul"
    )
    result = _run({"tool_name": "terminal", "tool_input": {"command": command}})
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_unrelated_terminal_and_non_create_tools_remain_fail_open() -> None:
    payloads = (
        {"tool_name": "terminal", "tool_input": {"command": "hermes kanban show t_123"}},
        {"tool_name": "terminal", "tool_input": {"command": "printf kanban-developer"}},
        {"tool_name": "kanban_comment", "tool_input": {"body": "completion_contract owner/repo"}},
    )
    for payload in payloads:
        result = _run(payload)
        assert result.returncode == 0, (result.stdout, result.stderr)


def test_hook_config_reuses_one_approved_command_idempotently_without_losing_siblings() -> None:
    helper = _load("specialist_contract_hook_config", CONFIG_HELPER)
    original = (
        "hooks:\n"
        "  pre_tool_call:\n"
        "    - matcher: other\n"
        "      command: python3 /tmp/other.py\n"
        "      timeout: 5\n"
        "  post_tool_call:\n"
        "    - matcher: post\n"
        "      command: python3 /tmp/post.py\n"
        "logging:\n"
        "  level: INFO\n"
    )
    command = "python3 /home/hermes/.hermes/scripts/kanban-block-kind-guard.py"
    rendered = helper.render(original, command)
    again = helper.render(rendered, command)
    assert rendered == again
    parsed = yaml.safe_load(rendered)
    entries = parsed["hooks"]["pre_tool_call"]
    guard_entries = [
        entry for entry in entries
        if "kanban-block-kind-guard.py" in str(entry.get("command", ""))
    ]
    assert {entry["matcher"] for entry in guard_entries} == {
        "kanban_block",
        "kanban_create",
        "terminal",
    }
    assert {entry["command"] for entry in guard_entries} == {command}
    assert all(entry["fail_closed"] is True for entry in guard_entries)
    assert not any(
        "kanban-specialist-completion-guard.py" in str(entry.get("command", ""))
        for entry in entries
    )
    assert parsed["hooks"]["post_tool_call"][0]["matcher"] == "post"
    assert parsed["logging"]["level"] == "INFO"


def test_deployer_dry_run_validates_specialist_guard_without_mutating_config() -> None:
    with tempfile.TemporaryDirectory(prefix="specialist-contract-deploy-") as directory:
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
        assert "kanban-block-kind-guard-core.py" in result.stdout
        assert "kanban-specialist-completion-guard.py" in result.stdout
        assert "lifecycle-guard matcher=kanban_create (fail_closed=true)" in result.stdout
        assert "shell-hook command path unchanged; no second consent command added" in result.stdout
        assert config.read_text(encoding="utf-8") == original
        assert not any(path.name.startswith(".deploy-candidate-") for path in scripts.iterdir())
