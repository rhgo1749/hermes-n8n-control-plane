#!/usr/bin/env python3
"""Fail-closed H4V3 guard for specialist Kanban creation contracts.

Hermes core supports PR-aware ``completion_contract`` values because some
standalone Kanban tasks are terminal only after exact-head GitHub acceptance.
H4V3 specialist tasks have a different lifecycle boundary: Investigator,
Developer, Reviewer, and Designer own bounded internal work and must be able to
finish while the linked PR is still open. GitHub merge/review state is
projected by the canonical edge on the Issue-backed root card.

This pre-tool policy rejects non-local completion contracts when a new task is
assigned to an H4V3 specialist profile. Omitted ``completion_contract`` is safe
because Hermes normalizes it to ``local-only``. PR URLs, repository names, and
head SHAs remain valid task-body / handoff provenance; they are not specialist
terminal policy.

It also rejects the ambiguous ``parents + initial_status=blocked`` creation
shape for specialists. An open parent is an ordinary dependency wait: Hermes
keeps that child on the ``todo`` path and promotes it when dependencies resolve.
``blocked`` is reserved for a separate human/operator hold, not a belt-and-
suspenders synonym for "not runnable yet".

The structured ``kanban_create`` tool is the canonical creation path. The
``terminal`` policy recognizes executable ``hermes kanban`` command segments
(including supported shell wrappers) and closes literal create/assign/reassign
bypasses without treating quoted documentation or echo/printf data as commands.
Reassignment reads only the canonical task row; unreadable/ambiguous state
fails closed and is never rewritten by this guard.

``evaluate_payload`` is importable by the already-approved lifecycle hook
wrapper so this policy does not need a second shell-hook command or a second
child Python process.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SPECIALIST_ASSIGNEES = frozenset(
    {
        "kanban-investigator",
        "kanban-developer",
        "kanban-reviewer",
        "kanban-designer",
    }
)
LOCAL_ONLY = "local-only"
_LOG_PATH = Path(
    os.environ.get(
        "KANBAN_SPECIALIST_COMPLETION_GUARD_LOG",
        "/home/hermes/.hermes/kanban/logs/specialist-completion-guard.log",
    )
)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_BINARIES = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_CONTROL_OPERATOR_CHARS = frozenset(";&|")
_SHELL_FLAG_ONLY = frozenset(
    {
        "-l", "--login", "-i", "--interactive", "-e", "-x", "-n", "--norc",
        "--noprofile", "--posix", "-v", "-s", "-p", "-f",
    }
)
_MAX_SHELL_DEPTH = 8


def _log(entry: Mapping[str, Any]) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), ensure_ascii=False) + "\n")
    except OSError:
        pass


def _block(message: str, *, assignee: str = "", source: str = "") -> int:
    print(json.dumps({"action": "block", "message": message}, ensure_ascii=False))
    _log(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "decision": "block",
            "assignee": assignee,
            "source": source,
            "message": message,
        }
    )
    return 2


def _specialist(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized if normalized in SPECIALIST_ASSIGNEES else None


def _contract_is_local(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == LOCAL_ONLY)


def _initial_status_is_blocked(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold() == "blocked"


def _structured_has_parent(raw_input: Mapping[str, Any]) -> bool:
    parent = raw_input.get("parent")
    if isinstance(parent, str) and parent.strip():
        return True
    parents = raw_input.get("parents")
    if isinstance(parents, str):
        return bool(parents.strip())
    if isinstance(parents, (list, tuple, set, frozenset)):
        return any(isinstance(value, str) and value.strip() for value in parents)
    return False


def _dependency_wait_diagnostic(assignee: str) -> str:
    return (
        f"H4V3 specialist task '{assignee}' must not use initial_status=blocked merely "
        "because it has an open parent dependency. Keep the parent relationship and omit "
        "initial_status=blocked; Hermes will keep the child on the normal todo dependency "
        "path and promote it when parents resolve. blocked is reserved for an explicit "
        "human/operator hold that ordinary parent completion will not resolve. "
        "No task mutation was performed."
    )


def _diagnostic(assignee: str, *, action: str = "create") -> str:
    retry = (
        "Retry the create call with local-only."
        if action == "create"
        else "Keep the task on a non-specialist profile or recover its completion contract through an explicit operator procedure before reassignment."
    )
    return (
        f"H4V3 specialist task '{assignee}' must use completion_contract=local-only "
        "(or omit the field at creation, which defaults to local-only). "
        "Investigator/Developer/Reviewer/Designer done is an internal specialist terminal state; "
        "GitHub PR acceptance/merge is owned by root-card edge reconciliation. "
        "Keep PR URL/head/repository as body or completion metadata evidence instead. "
        f"{retry} No task mutation was performed."
    )


def _homes() -> list[Path]:
    root = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    homes = [root]
    if root.parent.name == "profiles":
        homes.append(root.parent.parent)
    homes.append(Path.home() / ".hermes")
    return list(dict.fromkeys(homes))


def _current_slug(home: Path) -> str | None:
    try:
        value = (home / "kanban" / "current").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _board_db_path(board: str) -> Path:
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned:
        path = Path(pinned).expanduser()
        if path.is_file():
            return path
        raise RuntimeError(f"HERMES_KANBAN_DB does not point to a readable DB: {path}")

    requested = board.strip() or os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    for home in _homes():
        slug = requested or _current_slug(home) or "default"
        candidate = (
            home / "kanban.db"
            if slug == "default"
            else home / "kanban" / "boards" / slug / "kanban.db"
        )
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"could not resolve board DB for board '{requested or 'default'}'")


def _task_completion_contract(task_id: str, board: str) -> str | None:
    path = _board_db_path(board)
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
            if not {"id", "completion_contract"}.issubset(columns):
                raise RuntimeError("board DB tasks schema has no completion_contract")
            row = conn.execute(
                "SELECT completion_contract FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"task '{task_id}' was not found on the resolved board")
            value = row["completion_contract"]
            return None if value is None else str(value)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"could not read completion contract for task '{task_id}': {type(exc).__name__}: {exc}"
        ) from exc


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_control_operator(value: str) -> bool:
    return bool(value) and all(ch in _CONTROL_OPERATOR_CHARS for ch in value)


def _command_segments(tokens: list[str]) -> list[list[str]]:
    """Return chain commands for callers that only need the segment view."""
    return [segment for segment, _ in _command_chain(tokens)]


def _consume_env(segment: list[str], index: int) -> int | None:
    index += 1
    while index < len(segment):
        value = segment[index]
        if value == "--":
            return index + 1
        if _ENV_ASSIGN_RE.fullmatch(value):
            index += 1
            continue
        if value in {"-i", "--ignore-environment", "-0", "--null"}:
            index += 1
            continue
        if value in {"-u", "--unset", "-C", "--chdir"}:
            if index + 1 >= len(segment):
                return None
            index += 2
            continue
        if value.startswith(("--unset=", "--chdir=")):
            index += 1
            continue
        if (value.startswith("-u") or value.startswith("-C")) and len(value) > 2:
            index += 1
            continue
        if value.startswith("-"):
            return None
        return index
    return index


def _first_executable(segment: list[str]) -> int | None:
    index = 0
    while index < len(segment) and _ENV_ASSIGN_RE.fullmatch(segment[index]):
        index += 1
    while index < len(segment):
        name = Path(segment[index]).name
        if name == "env":
            next_index = _consume_env(segment, index)
            if next_index is None:
                return None
            index = next_index
            continue
        if name == "command":
            index += 1
            while index < len(segment) and segment[index] in {"-p", "--"}:
                index += 1
            continue
        if name == "nohup":
            index += 1
            if index < len(segment) and segment[index] == "--":
                index += 1
            continue
        return index
    return None


def _shell_inline_command(segment: list[str], index: int) -> str | None:
    rest = segment[index + 1 :]
    position = 0
    while position < len(rest):
        value = rest[position]
        if value in {"-c", "--command"}:
            return rest[position + 1] if position + 1 < len(rest) else None
        if value in _SHELL_FLAG_ONLY:
            position += 1
            continue
        if (
            value.startswith("-")
            and not value.startswith("--")
            and len(value) > 1
            and "c" in value[1:]
        ):
            return rest[position + 1] if position + 1 < len(rest) else None
        return None
    return None


def _command_chain(tokens: list[str]) -> list[tuple[list[str], str | None]]:
    """Split commands while preserving the operator before the next command."""
    chain: list[tuple[list[str], str | None]] = []
    current: list[str] = []
    for token in tokens:
        if _is_control_operator(token):
            if not current:
                raise RuntimeError("malformed shell control-flow operator")
            chain.append((current, token))
            current = []
            continue
        current.append(token)
    if current:
        chain.append((current, None))
    return chain


def _relevant_hermes_action(tokens: list[str], start: int) -> bool:
    try:
        kanban_index = tokens.index("kanban", start + 1)
    except ValueError:
        return False
    action_index = kanban_index + 1
    return action_index < len(tokens) and tokens[action_index].casefold() in {"create", "assign"}


def _contains_ambiguous_conditional(tokens: list[str]) -> bool:
    conditional_words = {"if", "then", "elif", "else", "fi", "case", "esac", "while", "until", "do", "done"}
    has_conditional = False
    for index, token in enumerate(tokens):
        if token not in conditional_words:
            continue
        previous_is_boundary = index == 0 or _is_control_operator(tokens[index - 1])
        next_is_boundary = index + 1 == len(tokens) or _is_control_operator(tokens[index + 1])
        if previous_is_boundary and (
            token in {"if", "then", "elif", "else", "case", "while", "until", "do"}
            or (token in {"fi", "esac", "done"} and next_is_boundary)
        ):
            has_conditional = True
            break
    if not has_conditional:
        return False
    return any(
        Path(token).name == "hermes" and _relevant_hermes_action(tokens, index)
        for index, token in enumerate(tokens)
    )


def _segment_has_hermes(segment: list[str], *, depth: int = 0) -> bool:
    if depth > _MAX_SHELL_DEPTH:
        raise RuntimeError("shell wrapper nesting exceeds the specialist guard limit")
    index = _first_executable(segment)
    if index is None or index >= len(segment):
        return False
    executable = Path(segment[index]).name
    if executable in _SHELL_BINARIES:
        inner = _shell_inline_command(segment, index)
        if inner is None:
            return False
        try:
            nested = _tokenize(inner)
        except ValueError as exc:
            raise RuntimeError(f"could not parse shell wrapper: {exc}") from exc
        return any(_segment_has_hermes(part, depth=depth + 1) for part, _ in _command_chain(nested))
    if executable != "hermes":
        return False
    return _relevant_hermes_action(segment, index)


def _literal_command_result(segment: list[str], *, depth: int = 0) -> bool | None:
    """Return a result only for predicates whose outcome is statically known."""
    if depth > _MAX_SHELL_DEPTH:
        raise RuntimeError("shell wrapper nesting exceeds the specialist guard limit")
    index = _first_executable(segment)
    if index is None or index >= len(segment):
        return True if segment and Path(segment[0]).name == "env" else None
    executable = Path(segment[index]).name
    if executable in {"true", ":"}:
        return True
    if executable == "false":
        return False
    if executable == "exit":
        if len(segment) == index + 1:
            return True
        try:
            return int(segment[index + 1]) == 0
        except ValueError:
            return None
    if executable == "cd":
        values = segment[index + 1 :]
        if len(values) != 1 or any(char in values[0] for char in "$`"):
            return None
        return Path(values[0]).expanduser().is_dir()
    if executable == "export":
        return all(_ENV_ASSIGN_RE.fullmatch(value) for value in segment[index + 1 :])
    if executable == "unset":
        return all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in segment[index + 1 :])
    if executable in {"echo", "printf"}:
        return True
    if executable in _SHELL_BINARIES:
        inner = _shell_inline_command(segment, index)
        if inner is None:
            return None
        try:
            nested = _tokenize(inner)
            nested_chain = _command_chain(nested)
        except ValueError as exc:
            raise RuntimeError(f"could not parse shell wrapper: {exc}") from exc
        if len(nested_chain) == 1 and nested_chain[0][1] is None:
            return _literal_command_result(nested_chain[0][0], depth=depth + 1)
    return None


def _hermes_kanban_invocations(
    command: str,
    *,
    depth: int = 0,
) -> list[tuple[str, list[str], str]]:
    """Return executable ``(action, args, board)`` invocations only."""
    if depth > _MAX_SHELL_DEPTH:
        raise RuntimeError("shell wrapper nesting exceeds the specialist guard limit")
    try:
        tokens = _tokenize(command)
    except ValueError as exc:
        raise RuntimeError(f"could not parse terminal command: {exc}") from exc
    if _contains_ambiguous_conditional(tokens):
        raise RuntimeError("ambiguous shell conditional reachability")

    invocations: list[tuple[str, list[str], str]] = []
    chain = _command_chain(tokens)
    reachable = True
    previous_result: bool | None = None
    for segment_index, (segment, operator) in enumerate(chain):
        if not reachable:
            if operator == ";":
                reachable = True
                previous_result = None
            elif operator in {"&&", "||"}:
                reachable = previous_result if operator == "&&" else not previous_result
            continue
        executable_index = _first_executable(segment)
        if executable_index is None or executable_index >= len(segment):
            previous_result = None
        else:
            executable = Path(segment[executable_index]).name
            if executable in _SHELL_BINARIES:
                inner = _shell_inline_command(segment, executable_index)
                if inner is not None:
                    invocations.extend(_hermes_kanban_invocations(inner, depth=depth + 1))
            elif executable == "hermes":
                try:
                    kanban_index = segment.index("kanban", executable_index + 1)
                except ValueError:
                    kanban_index = -1
                if kanban_index >= 0:
                    tail = segment[kanban_index + 1 :]
                    board = ""
                    position = 0
                    while position < len(tail):
                        value = tail[position]
                        if value == "--board":
                            if position + 1 >= len(tail):
                                raise RuntimeError("hermes kanban --board is missing a value")
                            board = tail[position + 1]
                            position += 2
                            continue
                        if value.startswith("--board="):
                            board = value.split("=", 1)[1]
                            position += 1
                            continue
                        break
                    if position < len(tail):
                        invocations.append((tail[position].casefold(), tail[position + 1 :], board))
            previous_result = _literal_command_result(segment, depth=depth)

        if operator is None:
            continue
        later_has_hermes = any(
            _segment_has_hermes(later, depth=depth)
            for later, _ in chain[segment_index + 1 :]
        )
        if operator == ";":
            reachable = True
            previous_result = None
        elif operator in {"&&", "||"}:
            if previous_result is None:
                if later_has_hermes:
                    raise RuntimeError("ambiguous shell command reachability")
                reachable = False
            else:
                reachable = previous_result if operator == "&&" else not previous_result
        elif later_has_hermes:
            raise RuntimeError("unsupported shell operator makes command reachability ambiguous")
    return invocations


def _option_values(args: list[str], option: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == option:
            if index + 1 >= len(args):
                raise RuntimeError(f"{option} is missing a value")
            values.append(args[index + 1])
            index += 2
            continue
        if value.startswith(option + "="):
            values.append(value.split("=", 1)[1])
        index += 1
    return values


def _evaluate_structured(payload: Mapping[str, Any]) -> int:
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, Mapping):
        return _block(
            "H4V3 specialist completion-contract gate failed closed: kanban_create "
            "tool_input must be an object. No task mutation was performed.",
            source="kanban_create",
        )
    assignee = _specialist(raw_input.get("assignee"))
    if assignee is None:
        return 0
    if _structured_has_parent(raw_input) and _initial_status_is_blocked(
        raw_input.get("initial_status")
    ):
        return _block(
            _dependency_wait_diagnostic(assignee),
            assignee=assignee,
            source="kanban_create:dependency_wait",
        )
    contract = raw_input.get("completion_contract")
    if _contract_is_local(contract):
        return 0
    return _block(_diagnostic(assignee), assignee=assignee, source="kanban_create")


def _evaluate_terminal_create(args: list[str], board: str) -> int:
    del board  # creation policy is local; no DB lookup is needed.
    assignees = _option_values(args, "--assignee")
    specialist = next((_specialist(value) for value in assignees if _specialist(value)), None)
    if specialist is None:
        return 0
    parents = _option_values(args, "--parent")
    initial_statuses = _option_values(args, "--initial-status")
    if parents and any(_initial_status_is_blocked(value) for value in initial_statuses):
        return _block(
            _dependency_wait_diagnostic(specialist),
            assignee=specialist,
            source="terminal:create:dependency_wait",
        )
    contracts = _option_values(args, "--completion-contract")
    if not contracts or all(_contract_is_local(value) for value in contracts):
        return 0
    return _block(
        _diagnostic(specialist),
        assignee=specialist,
        source="terminal:create",
    )


def _evaluate_terminal_assignment(action: str, args: list[str], board: str) -> int:
    if len(args) < 2:
        raise RuntimeError(f"hermes kanban {action} requires task_id and profile")
    task_id, profile = args[0], args[1]
    assignee = _specialist(profile)
    if assignee is None:
        return 0
    contract = _task_completion_contract(task_id, board)
    if _contract_is_local(contract):
        return 0
    return _block(
        _diagnostic(assignee, action="assign"),
        assignee=assignee,
        source=f"terminal:{action}",
    )


def _evaluate_terminal(payload: Mapping[str, Any]) -> int:
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, Mapping):
        return 0
    command = str(raw_input.get("command") or "")
    try:
        invocations = _hermes_kanban_invocations(command)
        for action, args, board in invocations:
            if action == "create":
                decision = _evaluate_terminal_create(args, board)
            elif action in {"assign", "reassign"}:
                decision = _evaluate_terminal_assignment(action, args, board)
            else:
                continue
            if decision != 0:
                return decision
        return 0
    except (OSError, RuntimeError) as exc:
        return _block(
            "H4V3 specialist completion-contract gate failed closed while classifying "
            f"terminal Kanban mutation: {type(exc).__name__}: {exc}. "
            "No task mutation was performed.",
            source="terminal",
        )


def evaluate_payload(payload: Mapping[str, Any]) -> int:
    """Evaluate one already-decoded pre_tool_call payload."""
    tool_name = str(payload.get("tool_name") or "")
    if tool_name == "kanban_create":
        return _evaluate_structured(payload)
    if tool_name == "terminal":
        return _evaluate_terminal(payload)
    return 0


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _block(
            "H4V3 specialist completion-contract gate failed closed on malformed "
            f"pre_tool_call payload: {type(exc).__name__}: {exc}. "
            "No task mutation was performed."
        )
    if not isinstance(payload, Mapping):
        return 0
    return evaluate_payload(payload)


if __name__ == "__main__":
    raise SystemExit(main())
