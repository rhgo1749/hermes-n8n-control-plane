#!/usr/bin/env python3
"""Focused parser regressions for the specialist completion-contract guard."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "automation/hermes/scripts/kanban-specialist-completion-guard.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("specialist_completion_parser", GUARD)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_quoted_documentation_is_not_an_executable_hermes_invocation() -> None:
    guard = _load_guard()
    commands = (
        "echo 'hermes kanban assign t_x kanban-developer'",
        "printf '%s' 'hermes kanban create x --assignee kanban-developer --completion-contract owner/repo'",
    )
    for command in commands:
        assert guard._hermes_kanban_invocations(command) == []


def test_direct_shell_and_compound_invocations_are_detected() -> None:
    guard = _load_guard()
    assert guard._hermes_kanban_invocations(
        "hermes kanban assign t_a kanban-developer"
    ) == [("assign", ["t_a", "kanban-developer"], "")]
    assert guard._hermes_kanban_invocations(
        "bash -lc 'hermes kanban reassign t_b kanban-reviewer --reclaim'"
    ) == [("reassign", ["t_b", "kanban-reviewer", "--reclaim"], "")]
    assert guard._hermes_kanban_invocations(
        "cd /tmp && hermes kanban --board ctrl-hangul assign t_c kanban-designer"
    ) == [("assign", ["t_c", "kanban-designer"], "ctrl-hangul")]


def test_literal_short_circuit_operators_skip_unreachable_invocations() -> None:
    guard = _load_guard()
    create = "hermes kanban create x --assignee kanban-developer"
    assert guard._hermes_kanban_invocations(f"false && {create}") == []
    assert guard._hermes_kanban_invocations(f"true || {create}") == []
    assert guard._hermes_kanban_invocations(f"true && {create}") == [
        ("create", ["x", "--assignee", "kanban-developer"], "")
    ]
    assert guard._hermes_kanban_invocations(f"false || {create}") == [
        ("create", ["x", "--assignee", "kanban-developer"], "")
    ]


def test_ambiguous_conditional_reachability_fails_closed() -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="reachability"):
        guard._hermes_kanban_invocations(
            "test -f /tmp/maybe && hermes kanban create x --assignee kanban-developer"
        )
    with pytest.raises(RuntimeError, match="conditional"):
        guard._hermes_kanban_invocations(
            "if test -f /tmp/maybe; then hermes kanban create x --assignee kanban-developer; fi"
        )
