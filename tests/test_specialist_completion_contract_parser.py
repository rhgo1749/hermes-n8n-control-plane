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
        "printf '%s' '( hermes kanban create x --assignee kanban-developer )'",
        "echo '{ hermes kanban assign t_x kanban-reviewer; }'",
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


@pytest.mark.parametrize(
    "command",
    [
        "echo \"$(hermes kanban create x --assignee kanban-developer)\"",
        "echo \"$(hermes kanban assign t_x kanban-reviewer)\"",
        "echo \"$(hermes kanban reassign t_x kanban-reviewer --reclaim)\"",
        "echo `hermes kanban create x --assignee kanban-developer`",
        "echo `hermes kanban assign t_x kanban-reviewer`",
        "echo `hermes kanban reassign t_x kanban-reviewer --reclaim`",
        "echo $(hermes kanban create x --assignee kanban-developer)",
        "echo $(hermes kanban assign t_x kanban-reviewer)",
        "echo $(hermes kanban reassign t_x kanban-reviewer --reclaim)",
    ],
)
def test_executable_command_substitutions_fail_closed_for_all_mutations(
    command: str,
) -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="command substitution"):
        guard._hermes_kanban_invocations(command)


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'echo \"$(hermes kanban create x --assignee kanban-developer)\"'",
        "env FOO=bar bash -lc 'echo `hermes kanban assign t_x kanban-reviewer`'",
        "env -- bash -lc 'echo $(hermes kanban reassign t_x kanban-reviewer --reclaim)'",
    ],
)
def test_nested_shell_wrappers_cannot_hide_command_substitutions(command: str) -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="command substitution"):
        guard._hermes_kanban_invocations(command)


@pytest.mark.parametrize(
    "substitution",
    [
        "$(hermes kanban create x --assignee kanban-developer)",
        "`hermes kanban assign t_x kanban-reviewer`",
    ],
)
def test_command_substitutions_preserve_literal_short_circuit_reachability(
    substitution: str,
) -> None:
    guard = _load_guard()
    mutation = f'echo "{substitution}"'
    assert guard._hermes_kanban_invocations(f"false && {mutation}") == []
    assert guard._hermes_kanban_invocations(f"true || {mutation}") == []
    for prefix in ("true &&", "false ||"):
        with pytest.raises(RuntimeError, match="command substitution"):
            guard._hermes_kanban_invocations(f"{prefix} {mutation}")


def test_unknown_predicate_cannot_hide_a_command_substitution() -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="reachability"):
        guard._hermes_kanban_invocations(
            "test -f /tmp/maybe && echo "
            '"$(hermes kanban create x --assignee kanban-developer)"'
        )


@pytest.mark.parametrize("operator", ["&&", "||"])
@pytest.mark.parametrize("board_option", ["--board ctrl-hangul", "--board=ctrl-hangul"])
@pytest.mark.parametrize(
    ("action", "args"),
    [
        ("create", "x --assignee kanban-developer"),
        ("assign", "t_x kanban-reviewer"),
        ("reassign", "t_x kanban-reviewer --reclaim"),
    ],
)
def test_unknown_predicate_lookahead_recognizes_board_qualified_mutations(
    operator: str, board_option: str, action: str, args: str
) -> None:
    guard = _load_guard()
    command = (
        f"test -f /tmp/maybe {operator} hermes kanban {board_option} "
        f"{action} {args}"
    )
    with pytest.raises(RuntimeError, match="reachability"):
        guard._hermes_kanban_invocations(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo $(( $(hermes kanban create x --assignee kanban-developer) + 1 ))",
        'echo "$(( $(hermes kanban --board ctrl-hangul assign t_x kanban-reviewer) + 1 ))"',
        "echo $(( `hermes kanban reassign t_x kanban-reviewer --reclaim` + 1 ))",
        'echo "$(( `hermes kanban --board=ctrl-hangul create x --assignee kanban-developer` + 1 ))"',
    ],
)
def test_arithmetic_expansions_cannot_hide_nested_command_substitutions(
    command: str,
) -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="command substitution"):
        guard._hermes_kanban_invocations(command)


def test_arithmetic_nested_command_substitution_depth_is_bounded() -> None:
    guard = _load_guard()
    command = "hermes kanban create x --assignee kanban-developer"
    for _ in range(guard._MAX_SHELL_DEPTH + 1):
        command = f'echo "$(( $({command}) + 1 ))"'

    with pytest.raises(RuntimeError, match="nesting exceeds"):
        guard._hermes_kanban_invocations(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo \"$(bash -lc 'hermes kanban create x --assignee kanban-developer')\"",
        "echo \"$(env FOO=bar bash -lc 'hermes kanban assign t_x kanban-reviewer')\"",
        "echo `env -- bash -lc 'hermes kanban reassign t_x kanban-reviewer --reclaim'`",
    ],
)
def test_shell_wrappers_nested_inside_substitutions_cannot_hide_mutations(
    command: str,
) -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="command substitution"):
        guard._hermes_kanban_invocations(command)


def test_unsupported_operator_after_a_reachable_substitution_fails_closed() -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="unsupported shell operator"):
        guard._hermes_kanban_invocations(
            'true && printf safe | echo "$(hermes kanban create x --assignee kanban-developer)"'
        )


@pytest.mark.parametrize(
    "command",
    [
        "echo '$(hermes kanban create x --assignee kanban-developer)'",
        'echo "\\$(hermes kanban assign t_x kanban-reviewer)"',
        'echo "\\`hermes kanban reassign t_x kanban-reviewer --reclaim\\`"',
        'echo "$(printf safe)"',
        "echo `printf safe`",
        'printf "%s" "hermes kanban create x --assignee kanban-developer"',
    ],
)
def test_literal_escaped_and_harmless_substitutions_remain_data(command: str) -> None:
    guard = _load_guard()
    assert guard._hermes_kanban_invocations(command) == []


@pytest.mark.parametrize(
    "command",
    [
        'echo "$(hermes kanban create x --assignee kanban-developer"',
        "echo `hermes kanban assign t_x kanban-reviewer",
    ],
)
def test_malformed_relevant_command_substitutions_fail_closed(command: str) -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="shell (?:command )?substitution|shell grouping"):
        guard._hermes_kanban_invocations(command)


def test_relevant_command_substitution_nesting_is_bounded() -> None:
    guard = _load_guard()
    command = "hermes kanban create x --assignee kanban-developer"
    for _ in range(guard._MAX_SHELL_DEPTH + 1):
        command = f'echo "$({command})"'

    with pytest.raises(RuntimeError, match="nesting exceeds"):
        guard._hermes_kanban_invocations(command)


@pytest.mark.parametrize(
    "command",
    [
        "( hermes kanban create x --assignee kanban-developer )",
        "true && ( hermes kanban assign t_x kanban-reviewer )",
        "{ hermes kanban reassign t_x kanban-reviewer --reclaim; }",
        "false || { hermes kanban create x --assignee kanban-developer; }",
    ],
)
def test_reachable_shell_grouping_fails_closed_for_all_mutation_families(
    command: str,
) -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="unsupported shell grouping"):
        guard._hermes_kanban_invocations(command)


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


@pytest.mark.parametrize("operator", ["&", "|", ";&", ";;&", "|||"])
def test_unsupported_shell_operators_fail_closed_after_short_circuit(
    operator: str,
) -> None:
    guard = _load_guard()
    create = "hermes kanban create x --assignee kanban-developer"
    for predicate in ("false &&", "true ||"):
        with pytest.raises(RuntimeError, match="unsupported shell operator"):
            guard._hermes_kanban_invocations(
                f"{predicate} {create} {operator} {create}"
            )


@pytest.mark.parametrize("operator", ["&", "|", ";&", ";;&", "|||"])
def test_unsupported_shell_operators_fail_closed_for_reassign(operator: str) -> None:
    guard = _load_guard()
    create = "hermes kanban create x --assignee kanban-developer"
    reassign = "hermes kanban reassign t_x kanban-reviewer --reclaim"
    with pytest.raises(RuntimeError, match="unsupported shell operator"):
        guard._hermes_kanban_invocations(f"false && {create} {operator} {reassign}")


@pytest.mark.parametrize("operator", ["&", "|", ";&", ";;&", "|||"])
def test_unsupported_shell_operators_fail_closed_for_assign(operator: str) -> None:
    guard = _load_guard()
    create = "hermes kanban create x --assignee kanban-developer"
    assign = "hermes kanban assign t_x kanban-reviewer"
    with pytest.raises(RuntimeError, match="unsupported shell operator"):
        guard._hermes_kanban_invocations(f"false && {create} {operator} {assign}")


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


def test_ambiguous_conditional_reachability_fails_closed_for_reassign() -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="conditional"):
        guard._hermes_kanban_invocations(
            "if true; then hermes kanban reassign t_x kanban-reviewer --reclaim; fi"
        )


@pytest.mark.parametrize(
    "command",
    [
        "if true; then bash -lc \"hermes kanban create x --assignee kanban-developer --completion-contract owner/repo\"; fi",
        "if true; then env bash -lc \"hermes kanban reassign t_x kanban-reviewer --reclaim\"; fi",
    ],
)
def test_reachable_conditional_shell_wrappers_fail_closed(command: str) -> None:
    guard = _load_guard()
    with pytest.raises(RuntimeError, match="conditional"):
        guard._hermes_kanban_invocations(command)
