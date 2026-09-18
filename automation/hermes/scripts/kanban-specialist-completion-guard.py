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
Unsupported shell grouping is not flattened into the supported command-chain
grammar: a relevant mutation inside ``(...)`` or ``{...;}`` fails closed before
the terminal command can execute. Search candidates and selectors use the same
canonical task_events admission ledger on every surface, and a terminal
create that repeats --body/--assignee/--idempotency-key with differing
values fails closed before admission because argparse would mutate with
the last value while the admitted payload could describe another. The
ledger atomically
reserves two candidate slots, a 32000-token cumulative budget, two retry
reservations, and a 1800-second dispatcher cap. Reassignment reads only the
canonical task row; unreadable/ambiguous state fails closed and is never
rewritten by this guard.

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
_INVESTIGATION_SEARCH_SCHEMA = "h4v3-investigation-search-v1"
_INVESTIGATION_SEARCH_PHASES = frozenset({"candidate", "selector"})
_INVESTIGATION_SEARCH_CANDIDATES = frozenset({"A", "B"})
_INVESTIGATION_SEARCH_ADMISSION_KIND = "investigation_search_admission"
_INVESTIGATION_SEARCH_CANDIDATE_KIND = "investigation_search_candidate_admitted"
_INVESTIGATION_SEARCH_SELECTOR_KIND = "investigation_search_selector_admitted"
_INVESTIGATION_SEARCH_MAX_CANDIDATES = 2
_INVESTIGATION_SEARCH_MAX_EXPANSIONS = 1
_INVESTIGATION_SEARCH_MAX_TOTAL_TOKENS = 32_000
_INVESTIGATION_SEARCH_MAX_RETRIES = 2
_INVESTIGATION_SEARCH_MAX_RUNTIME_SECONDS = 7200
_KANBAN_TASK_RETRY_LIMIT = 5
_INVESTIGATION_SEARCH_TRIGGER_CODES = frozenset(
    {
        "LOW_CONFIDENCE_OR_BLOCKING_UNKNOWN",
        "IMPLEMENTATION_OR_REWORK_ROUND_GE_2",
        "RUNTIME_TEST_CONTRADICTION",
        "TRUSTED_REVIEWER_MODEL_REFRESH",
        "COMPETING_BOUNDARIES_UNRESOLVED",
        "NEW_EQUIVALENCE_CLASS_BYPASS_AFTER_PASS",
        "ACCEPTANCE_PASS_RUNTIME_ORACLE_FALSE_NEGATIVE",
    }
)
_LOG_PATH = Path(
    os.environ.get(
        "KANBAN_SPECIALIST_COMPLETION_GUARD_LOG",
        "/home/hermes/.hermes/kanban/logs/specialist-completion-guard.log",
    )
)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_BINARIES = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_CONTROL_OPERATOR_CHARS = frozenset(";&|")
_SUPPORTED_SHELL_OPERATORS = frozenset({";", "&&", "||"})
_GROUPING_OPEN_TO_CLOSE = {"(": ")", "{": "}"}
_SHELL_FLAG_ONLY = frozenset(
    {
        "-l", "--login", "-i", "--interactive", "-e", "-x", "-n", "--norc",
        "--noprofile", "--posix", "-v", "-s", "-p", "-f",
    }
)
_MAX_SHELL_DEPTH = 8
_DIRECT_WORKER_TURN_TASK_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


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


def _extract_hermes_action(
    tokens: list[str], start: int
) -> tuple[str, list[str], str] | None:
    try:
        kanban_index = tokens.index("kanban", start + 1)
    except ValueError:
        return None
    tail = tokens[kanban_index + 1 :]
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
    if position >= len(tail):
        return None
    return tail[position].casefold(), tail[position + 1 :], board


def _relevant_hermes_action(tokens: list[str], start: int) -> bool:
    invocation = _extract_hermes_action(tokens, start)
    return invocation is not None and invocation[0] in {
        "create",
        "assign",
        "reassign",
    }


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
    if any(
        Path(token).name == "hermes" and _relevant_hermes_action(tokens, index)
        for index, token in enumerate(tokens)
    ):
        return True
    for segment, _ in _command_chain(tokens):
        for index, token in enumerate(segment):
            if token not in {"if", "then", "elif", "else", "case", "while", "until", "do"}:
                continue
            nested = segment[index + 1 :]
            if nested and _segment_has_hermes(nested):
                return True
    return False


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
        return _command_contains_relevant_hermes(inner, depth=depth + 1)
    if executable != "hermes":
        return False
    return _relevant_hermes_action(segment, index)


def _scan_shell_groupings(
    command: str,
) -> tuple[list[str], list[tuple[str, bool, int]], bool]:
    """Return grouping bodies, command substitutions, and malformed status.

    The scanner is deliberately lexical rather than a shell interpreter.  A
    substitution body is returned with a per-body malformed flag so callers
    can reject a relevant incomplete expansion without rejecting harmless
    expansion syntax.  Single-quoted and escaped data stays inert; the body
    of a command substitution is scanned in its own quote context because
    double quotes do not make ``$()`` or backticks inert.
    """
    groups: list[str] = []
    substitutions: list[tuple[str, bool, int]] = []

    def consume_arithmetic(
        start: int,
        *,
        depth: int,
        record_substitutions: bool,
        segment_index: int,
    ) -> tuple[int, bool]:
        """Return the end of a ``$((...))`` expansion without interpreting it."""
        arithmetic_depth = 0
        index = start
        malformed = False
        while index < len(command):
            value = command[index]
            if value == "\\":
                escaped = command[index + 1 : index + 2]
                if escaped == "`":
                    end, body, substitution_malformed = consume_substitution(
                        index + 1,
                        "`",
                        depth=depth + 1,
                        record_substitutions=True,
                    )
                    substitutions.append(
                        (body, substitution_malformed, segment_index)
                    )
                    malformed |= substitution_malformed
                    index = len(command) if substitution_malformed else end + 1
                else:
                    index += 2
                continue
            if value == "$" and command[index + 1 : index + 3] == "((":
                nested_end, nested_malformed = consume_arithmetic(
                    index + 1,
                    depth=depth,
                    record_substitutions=record_substitutions,
                    segment_index=segment_index,
                )
                malformed |= nested_malformed
                if nested_malformed:
                    return len(command), True
                index = nested_end + 1
                continue
            delimiter: str | None = None
            if value == "$" and command[index : index + 2] == "$(":
                delimiter = "$("
            elif value == "`":
                delimiter = "`"
            if delimiter is not None:
                end, body, substitution_malformed = consume_substitution(
                    index, delimiter, depth=depth + 1
                )
                if record_substitutions:
                    substitutions.append(
                        (body, substitution_malformed, segment_index)
                    )
                malformed |= substitution_malformed
                if substitution_malformed:
                    return len(command), True
                index = end + 1
                continue
            if value == "(":
                arithmetic_depth += 1
            elif value == ")":
                arithmetic_depth -= 1
                if arithmetic_depth == 0:
                    return index, malformed
            index += 1
        return len(command), True

    def consume_substitution(
        start: int,
        delimiter: str,
        *,
        depth: int,
        record_substitutions: bool = False,
    ) -> tuple[int, str, bool]:
        if depth > _MAX_SHELL_DEPTH:
            raise RuntimeError("shell command substitution nesting exceeds the specialist guard limit")
        body_start = start + (2 if delimiter == "$(" else 1)
        end, malformed, closed = scan_fragment(
            body_start,
            ")" if delimiter == "$(" else "`",
            depth=depth,
            record_substitutions=record_substitutions,
        )
        return end, command[body_start:end], malformed or not closed

    def scan_fragment(
        start: int,
        closing: str | None,
        *,
        depth: int = 0,
        record_substitutions: bool = False,
    ) -> tuple[int, bool, bool]:
        fragment_malformed = False
        stack: list[tuple[str, int]] = []
        quote: str | None = None
        segment_index = 0
        index = start
        while index < len(command):
            value = command[index]
            if quote == "'":
                if value == "'":
                    quote = None
                index += 1
                continue
            if quote == '"':
                if value == "\\":
                    index += 2
                    continue
                if value == '"':
                    quote = None
                    index += 1
                    continue
                if value == "$" and command[index + 1 : index + 3] == "((":
                    arithmetic_end, arithmetic_malformed = consume_arithmetic(
                        index + 1,
                        depth=depth,
                        record_substitutions=record_substitutions,
                        segment_index=segment_index,
                    )
                    fragment_malformed |= arithmetic_malformed
                    if arithmetic_malformed:
                        index = len(command)
                    else:
                        index = arithmetic_end + 1
                    continue
                if value == "$" and command[index : index + 2] == "$(":
                    end, body, substitution_malformed = consume_substitution(
                        index, "$(", depth=depth + 1
                    )
                    if record_substitutions and not stack:
                        substitutions.append(
                            (body, substitution_malformed, segment_index)
                        )
                    fragment_malformed |= substitution_malformed
                    index = len(command) if substitution_malformed else end + 1
                    continue
                if value == "`":
                    end, body, substitution_malformed = consume_substitution(
                        index, "`", depth=depth + 1
                    )
                    if record_substitutions and not stack:
                        substitutions.append(
                            (body, substitution_malformed, segment_index)
                        )
                    fragment_malformed |= substitution_malformed
                    index = len(command) if substitution_malformed else end + 1
                    continue
                index += 1
                continue
            if value == "'":
                quote = "'"
                index += 1
                continue
            if value == '"':
                quote = '"'
                index += 1
                continue
            if value == "\\":
                escaped = command[index + 1 : index + 2]
                if escaped == "$" and command[index + 1 : index + 3] == "$(":
                    end, _, _ = consume_substitution(
                        index + 1, "$(", depth=depth + 1
                    )
                    index = end + 1
                elif escaped == "`":
                    if closing is not None:
                        end, body, substitution_malformed = consume_substitution(
                            index + 1,
                            "`",
                            depth=depth + 1,
                            record_substitutions=True,
                        )
                        substitutions.append(
                            (body, substitution_malformed, segment_index)
                        )
                        fragment_malformed |= substitution_malformed
                        index = (
                            len(command)
                            if substitution_malformed
                            else end + 1
                        )
                    else:
                        end, _, _ = consume_substitution(
                            index + 1, "`", depth=depth + 1
                        )
                        index = end + 1
                else:
                    index += 2
                continue
            if closing == "`" and value == "`":
                return index, fragment_malformed, True
            if value == "$" and command[index + 1 : index + 3] == "((":
                arithmetic_end, arithmetic_malformed = consume_arithmetic(
                    index + 1,
                    depth=depth,
                    record_substitutions=record_substitutions,
                    segment_index=segment_index,
                )
                fragment_malformed |= arithmetic_malformed
                if arithmetic_malformed:
                    index = len(command)
                else:
                    index = arithmetic_end + 1
                continue
            if value == "$" and command[index : index + 2] == "$(":
                end, body, substitution_malformed = consume_substitution(
                    index, "$(", depth=depth + 1
                )
                if record_substitutions and not stack:
                    substitutions.append((body, substitution_malformed, segment_index))
                fragment_malformed |= substitution_malformed
                index = len(command) if substitution_malformed else end + 1
                continue
            if value == "`":
                end, body, substitution_malformed = consume_substitution(
                    index, "`", depth=depth + 1
                )
                if record_substitutions and not stack:
                    substitutions.append((body, substitution_malformed, segment_index))
                fragment_malformed |= substitution_malformed
                index = len(command) if substitution_malformed else end + 1
                continue
            if value in _GROUPING_OPEN_TO_CLOSE:
                stack.append((value, index + 1))
            elif value in {")", "}"}:
                if stack:
                    if _GROUPING_OPEN_TO_CLOSE[stack[-1][0]] != value:
                        fragment_malformed = True
                    else:
                        _, body_start = stack.pop()
                        groups.append(command[body_start:index])
                elif closing == value:
                    return index, fragment_malformed, True
                else:
                    fragment_malformed = True
            elif value in _CONTROL_OPERATOR_CHARS:
                operator_start = index
                while (
                    index < len(command)
                    and command[index] in _CONTROL_OPERATOR_CHARS
                ):
                    index += 1
                if index > operator_start:
                    segment_index += 1
                    continue
            index += 1
        if quote is not None:
            fragment_malformed = True
        if stack:
            fragment_malformed = True
            groups.extend(command[group_start:] for _, group_start in stack)
        if closing is not None:
            fragment_malformed = True
        return len(command), fragment_malformed, False

    _, malformed, _ = scan_fragment(0, None, record_substitutions=True)
    return groups, substitutions, malformed


def _command_contains_relevant_hermes(command: str, *, depth: int = 0) -> bool:
    """Return whether a bounded command body can invoke a relevant action.

    The invocation walker remains the single owner of shell reachability.  This
    predicate is only used by grouping/operator lookahead, so an ambiguous or
    malformed body is conservatively treated as relevant rather than parsed by
    a second command-chain implementation.
    """
    if depth > _MAX_SHELL_DEPTH:
        raise RuntimeError("shell wrapper nesting exceeds the specialist guard limit")
    try:
        invocations = _hermes_kanban_invocations(command, depth=depth)
    except RuntimeError:
        return True
    return any(action in {"create", "assign", "reassign"} for action, _, _ in invocations)


def _substitution_records_contain_relevant(
    substitutions: list[tuple[str, bool, int]],
    *,
    depth: int,
    segment_index: int | None = None,
    after_segment: int | None = None,
) -> bool:
    for body, malformed, body_segment in substitutions:
        if segment_index is not None and body_segment != segment_index:
            continue
        if after_segment is not None and body_segment <= after_segment:
            continue
        if not _command_contains_relevant_hermes(body, depth=depth + 1):
            continue
        if malformed:
            raise RuntimeError(
                "malformed shell command substitution contains a relevant "
                "specialist Kanban mutation"
            )
        return True
    return False


def _substitutions_contain_relevant_hermes(
    command: str, *, depth: int = 0
) -> bool:
    _, substitutions, _ = _scan_shell_groupings(command)
    return _substitution_records_contain_relevant(substitutions, depth=depth)


def _groups_contain_relevant_hermes(command: str, *, depth: int = 0) -> bool:
    groups, _, _ = _scan_shell_groupings(command)
    return any(
        _command_contains_relevant_hermes(group, depth=depth + 1)
        for group in groups
    )


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
    groups, substitutions, _ = _scan_shell_groupings(command)
    if any(
        _command_contains_relevant_hermes(group, depth=depth + 1)
        for group in groups
    ):
        raise RuntimeError(
            "unsupported shell grouping contains a relevant specialist Kanban mutation"
        )
    if _contains_ambiguous_conditional(tokens):
        raise RuntimeError("ambiguous shell conditional reachability")

    invocations: list[tuple[str, list[str], str]] = []
    chain = _command_chain(tokens)
    reachable = True
    previous_result: bool | None = None
    for segment_index, (segment, operator) in enumerate(chain):
        if (
            (
                operator is not None
                and operator not in _SUPPORTED_SHELL_OPERATORS
                and any(
                    _segment_has_hermes(later, depth=depth)
                    for later, _ in chain[segment_index + 1 :]
                )
            )
            or (
                operator is not None
                and operator not in _SUPPORTED_SHELL_OPERATORS
                and _substitution_records_contain_relevant(
                    substitutions, depth=depth, after_segment=segment_index
                )
            )
        ):
            raise RuntimeError(
                "unsupported shell operator makes command reachability ambiguous"
            )
        if not reachable:
            if operator == ";":
                reachable = True
                previous_result = None
            elif operator in {"&&", "||"}:
                reachable = previous_result if operator == "&&" else not previous_result
            continue
        if _substitution_records_contain_relevant(
            substitutions, depth=depth, segment_index=segment_index
        ):
            raise RuntimeError(
                "unsupported shell command substitution contains a relevant "
                "specialist Kanban mutation"
            )
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
                invocation = _extract_hermes_action(segment, executable_index)
                if invocation is not None:
                    invocations.append(invocation)
            previous_result = _literal_command_result(segment, depth=depth)

        if operator is None:
            continue
        later_has_hermes = any(
            _segment_has_hermes(later, depth=depth)
            for later, _ in chain[segment_index + 1 :]
        ) or _substitution_records_contain_relevant(
            substitutions, depth=depth, after_segment=segment_index
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


_SINGLE_VALUE_TERMINAL_OPTIONS = ("--body", "--assignee", "--idempotency-key")


def _require_unique_terminal_options(args: list[str]) -> None:
    """Reject conflicting repeated options before any admission decision.

    Canonical argparse mutates with the LAST value of a repeated option, so
    an admitted payload built from a different value would describe a task
    that is not the one about to be created. Identical repeats are
    semantically harmless; differing values are ambiguous and fail closed.
    """
    for option in _SINGLE_VALUE_TERMINAL_OPTIONS:
        values = _option_values(args, option)
        if len(set(values)) > 1:
            raise RuntimeError(
                f"conflicting duplicate {option} option values are ambiguous: "
                "canonical argparse would mutate with the last value"
            )


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _search_block(message: str, assignee: str) -> int:
    return _block(
        "H4V3 bounded Investigator search admission failed closed: " + message
        + " No task mutation was performed.",
        assignee=assignee,
        source="kanban_create:investigation_search",
    )


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _search_budget(marker: Mapping[str, Any]) -> tuple[dict[str, int] | None, str | None]:
    budget = marker.get("budget")
    if not isinstance(budget, Mapping):
        return None, "an explicit budget object is required"
    required = (
        "max_candidates",
        "max_expansions",
        "max_runtime_seconds",
        "max_total_tokens",
        "max_retries",
    )
    parsed: dict[str, int] = {}
    for field in required:
        value = budget.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None, f"budget.{field} must be a positive integer"
        parsed[field] = value
    if parsed["max_candidates"] != _INVESTIGATION_SEARCH_MAX_CANDIDATES:
        return None, "budget.max_candidates must be exactly two"
    if parsed["max_expansions"] != _INVESTIGATION_SEARCH_MAX_EXPANSIONS:
        return None, "budget.max_expansions must be exactly one"
    if parsed["max_total_tokens"] > _INVESTIGATION_SEARCH_MAX_TOTAL_TOKENS:
        return None, "budget.max_total_tokens exceeds the 32000-token search cap"
    if parsed["max_retries"] != _INVESTIGATION_SEARCH_MAX_RETRIES:
        return None, "budget.max_retries must be exactly two cumulative retry reservations"
    if parsed["max_runtime_seconds"] > _INVESTIGATION_SEARCH_MAX_RUNTIME_SECONDS:
        return None, "budget.max_runtime_seconds exceeds the 7200-second search cap"
    return parsed, None


def _search_trigger_error(marker: Mapping[str, Any]) -> str | None:
    raw_codes = marker.get("trigger_codes")
    if raw_codes is None:
        raw_codes = marker.get("triggers")
    if not isinstance(raw_codes, (list, tuple, set, frozenset)) or not raw_codes:
        return "at least one explicit investigation trigger code is required"
    codes = [code.strip() for code in raw_codes if isinstance(code, str) and code.strip()]
    if len(codes) != len(raw_codes) or any(
        code not in _INVESTIGATION_SEARCH_TRIGGER_CODES for code in codes
    ):
        return "investigation trigger codes must use the canonical bounded-search allowlist"
    return None


def _search_connection(board: str, *, read_only: bool = False) -> sqlite3.Connection:
    path = _board_db_path(board)
    if read_only:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
    else:
        conn = sqlite3.connect(str(path), timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _direct_worker_task_id(payload: Mapping[str, Any], board: str) -> str | None:
    """Recover dispatcher-owned worker identity after Hermes shell-hook env scrubbing.

    Hermes intentionally removes ``HERMES_KANBAN_TASK`` from every child-process env,
    including trusted ``pre_tool_call`` shell hooks.  The native structured
    ``kanban_create`` surface is not exposed to ``delegate_task`` children, but keep
    this recovery fail-closed anyway: require the direct one-shot turn UUID, the
    Kanban session source, and an exact durable workspace/PID/run binding.
    """
    current = _nonempty_string(os.environ.get("HERMES_KANBAN_TASK"))
    if current:
        return current
    if os.environ.get("HERMES_SESSION_SOURCE") != "kanban":
        return None
    extra = payload.get("extra")
    turn_task_id = _nonempty_string(extra.get("task_id")) if isinstance(extra, Mapping) else None
    if not turn_task_id or not _DIRECT_WORKER_TURN_TASK_RE.fullmatch(turn_task_id):
        return None
    workspace = _nonempty_string(os.environ.get("HERMES_KANBAN_WORKSPACE"))
    if not workspace:
        return None

    conn: sqlite3.Connection | None = None
    try:
        conn = _search_connection(board, read_only=True)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
        required = {"id", "status", "workspace_path", "worker_pid", "current_run_id"}
        if not required.issubset(columns):
            raise RuntimeError("board DB tasks schema cannot prove direct worker identity")
        rows = conn.execute(
            "SELECT id, status, workspace_path, worker_pid, current_run_id "
            "FROM tasks WHERE workspace_path = ?",
            (workspace,),
        ).fetchall()
        if len(rows) != 1:
            raise RuntimeError("worker workspace does not resolve to exactly one durable task")
        row = rows[0]
        if str(row["status"] or "") != "running" or row["current_run_id"] is None:
            raise RuntimeError("worker workspace is not bound to a live durable run")
        try:
            worker_pid = int(row["worker_pid"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("worker workspace has no durable worker PID") from exc
        if worker_pid != os.getppid():
            raise RuntimeError("shell hook parent PID does not match the durable worker PID")
        return _nonempty_string(row["id"])
    finally:
        if conn is not None:
            conn.close()


def _search_require_schema(conn: sqlite3.Connection) -> None:
    required_tables = {"tasks", "task_links", "task_events"}
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = required_tables - tables
    if missing:
        raise RuntimeError(
            "board DB is missing durable admission tables: " + ", ".join(sorted(missing))
        )
    task_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")
    }
    if not {"id", "assignee", "idempotency_key"}.issubset(task_columns):
        raise RuntimeError("board DB tasks schema cannot bind search tasks")
    event_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(task_events)")
    }
    if not {"task_id", "run_id", "kind", "payload", "created_at"}.issubset(event_columns):
        raise RuntimeError("board DB task_events schema cannot record search admission")


def _search_task_descends_from(
    conn: sqlite3.Connection, root_task_id: str, task_id: str,
) -> bool:
    if root_task_id == task_id:
        return True
    pending = [root_task_id]
    visited = {root_task_id}
    while pending:
        parent_id = pending.pop()
        for row in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?",
            (parent_id,),
        ):
            child_id = str(row[0])
            if child_id == task_id:
                return True
            if child_id not in visited:
                visited.add(child_id)
                pending.append(child_id)
    return False


def _search_event_payload(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _search_payload_identity(
    payload: Mapping[str, Any], *, root_task_id: str, search_id: str,
) -> bool:
    return (
        payload.get("schema_id") == _INVESTIGATION_SEARCH_SCHEMA
        and payload.get("search_id") == search_id
        and payload.get("root_task_id") == root_task_id
    )


def _search_validate_admission_payload(
    payload: Mapping[str, Any], *, root_task_id: str, search_id: str,
    budget: Mapping[str, int],
) -> None:
    if not _search_payload_identity(payload, root_task_id=root_task_id, search_id=search_id):
        raise RuntimeError("malformed investigation search admission identity")
    if not _nonempty_string(payload.get("idempotency_key")):
        raise RuntimeError("investigation search admission is missing its idempotency key")
    if payload.get("state") != "active" or payload.get("budget") != dict(budget):
        raise RuntimeError("investigation search admission has inconsistent state or budget")


def _search_validate_candidate_events(
    candidate_events: list[dict[str, Any]], *, root_task_id: str, search_id: str,
    budget: Mapping[str, int],
) -> None:
    candidate_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for payload in candidate_events:
        if not _search_payload_identity(payload, root_task_id=root_task_id, search_id=search_id):
            raise RuntimeError("malformed investigation search candidate identity")
        candidate_id = payload.get("candidate_id")
        idempotency_key = _nonempty_string(payload.get("idempotency_key"))
        token_reservation = payload.get("token_reservation")
        retry_reservation = payload.get("retry_reservation")
        if candidate_id not in _INVESTIGATION_SEARCH_CANDIDATES:
            raise RuntimeError("investigation search candidate ledger contains an invalid candidate ID")
        if not idempotency_key:
            raise RuntimeError("investigation search candidate ledger is missing an idempotency key")
        if (
            not isinstance(token_reservation, int)
            or isinstance(token_reservation, bool)
            or token_reservation <= 0
            or not isinstance(retry_reservation, int)
            or isinstance(retry_reservation, bool)
            or retry_reservation <= 0
        ):
            raise RuntimeError("investigation search candidate ledger contains an invalid reservation")
        token_reservation_value = token_reservation
        retry_reservation_value = retry_reservation
        if (
            token_reservation_value > budget["max_total_tokens"]
            or retry_reservation_value > budget["max_retries"]
        ):
            raise RuntimeError("investigation search candidate ledger reservation exceeds its budget")
        if candidate_id in candidate_ids or idempotency_key in idempotency_keys:
            raise RuntimeError("investigation search candidate ledger contains a duplicate admission")
        candidate_ids.add(candidate_id)
        idempotency_keys.add(idempotency_key)


def _search_validate_selector_events(
    selector_events: list[dict[str, Any]], *, root_task_id: str, search_id: str,
) -> None:
    idempotency_keys: set[str] = set()
    pending_count = 0
    closed_count = 0
    for payload in selector_events:
        if not _search_payload_identity(payload, root_task_id=root_task_id, search_id=search_id):
            raise RuntimeError("malformed investigation search selector identity")
        idempotency_key = _nonempty_string(payload.get("idempotency_key"))
        state = payload.get("state")
        raw_candidate_task_ids = payload.get("candidate_task_ids")
        candidate_task_ids = _string_list(raw_candidate_task_ids)
        if (
            not idempotency_key
            or state not in {"pending", "closed"}
            or not isinstance(raw_candidate_task_ids, (list, tuple, set, frozenset))
            or not candidate_task_ids
            or len(candidate_task_ids) != len(raw_candidate_task_ids)
            or len(candidate_task_ids) != len(set(candidate_task_ids))
        ):
            raise RuntimeError("investigation search selector ledger contains an invalid admission")
        if idempotency_key in idempotency_keys:
            raise RuntimeError("investigation search selector ledger contains a duplicate admission")
        idempotency_keys.add(idempotency_key)
        if state == "pending":
            pending_count += 1
        else:
            closed_count += 1
    if pending_count > 1:
        raise RuntimeError("investigation search selector ledger contains duplicate pending admissions")
    if closed_count > 1:
        raise RuntimeError("investigation search selector ledger contains duplicate closed admissions")


def _search_events(
    conn: sqlite3.Connection, root_task_id: str, kind: str | None = None,
) -> list[tuple[sqlite3.Row, dict[str, Any]]]:
    if kind is None:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? "
            "AND kind IN (?, ?, ?) ORDER BY created_at ASC, id ASC",
            (
                root_task_id,
                _INVESTIGATION_SEARCH_ADMISSION_KIND,
                _INVESTIGATION_SEARCH_CANDIDATE_KIND,
                _INVESTIGATION_SEARCH_SELECTOR_KIND,
            ),
        )
    else:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY created_at ASC, id ASC",
            (root_task_id, kind),
        )
    return [(row, _search_event_payload(row)) for row in rows]


def _search_root_authorized(
    conn: sqlite3.Connection, root_task_id: str, current_task_id: str,
) -> None:
    root = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (root_task_id,)
    ).fetchone()
    current = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (current_task_id,)
    ).fetchone()
    if root is None or current is None:
        raise RuntimeError("root/current task is missing from the resolved board")
    if str(root["assignee"] or "").strip().casefold() != "kanban-main":
        raise RuntimeError("root task is not assigned to kanban-main")
    if str(current["assignee"] or "").strip().casefold() != "kanban-main":
        raise RuntimeError("only a kanban-main task may open a search admission")
    if not _search_task_descends_from(conn, root_task_id, current_task_id):
        raise RuntimeError("current task is not a descendant of the declared search root")


def _search_insert_event(
    conn: sqlite3.Connection, root_task_id: str, kind: str, payload: Mapping[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, NULL, ?, ?, ?)",
        (root_task_id, kind, json.dumps(dict(payload), sort_keys=True), int(time.time())),
    )


def _search_active_contexts(
    conn: sqlite3.Connection, current_task_id: str,
) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    admission_roots = conn.execute(
        "SELECT DISTINCT task_id FROM task_events WHERE kind = ?",
        (_INVESTIGATION_SEARCH_ADMISSION_KIND,),
    )
    for root_row in admission_roots:
        root_task_id = str(root_row[0])
        if not _search_task_descends_from(conn, root_task_id, current_task_id):
            continue
        for _, payload in _search_events(
            conn, root_task_id, _INVESTIGATION_SEARCH_ADMISSION_KIND
        ):
            search_id = _nonempty_string(payload.get("search_id"))
            if not search_id:
                raise RuntimeError("investigation search admission is missing its search ID")
            budget, budget_error = _search_budget(payload)
            if budget_error is not None or budget is None:
                raise RuntimeError("investigation search admission has an invalid budget")
            _search_validate_admission_payload(
                payload,
                root_task_id=root_task_id,
                search_id=search_id,
                budget=budget,
            )
            candidate_events = [
                candidate_payload
                for row, candidate_payload in _search_events(
                    conn, root_task_id, _INVESTIGATION_SEARCH_CANDIDATE_KIND
                )
                if candidate_payload.get("search_id") == search_id
            ]
            _search_validate_candidate_events(
                candidate_events,
                root_task_id=root_task_id,
                search_id=search_id,
                budget=budget,
            )
            selector_events = [
                selector_payload
                for row, selector_payload in _search_events(
                    conn, root_task_id, _INVESTIGATION_SEARCH_SELECTOR_KIND
                )
                if selector_payload.get("search_id") == search_id
            ]
            _search_validate_selector_events(
                selector_events,
                root_task_id=root_task_id,
                search_id=search_id,
            )
            selector_exists = any(
                _nonempty_string(selector_payload.get("search_id")) == search_id
                and selector_payload.get("state") == "closed"
                for _, selector_payload in _search_events(
                    conn, root_task_id, _INVESTIGATION_SEARCH_SELECTOR_KIND
                )
            )
            if not selector_exists:
                active.append(payload)
    return active


def _search_admit(
    raw_input: Mapping[str, Any], marker: Mapping[str, Any], assignee: str,
    budget: Mapping[str, int], phase: str, candidate_id: str | None,
    current_task_id: str | None = None,
) -> int:
    board = str(raw_input.get("board") or "")
    current_task_id = current_task_id or _nonempty_string(os.environ.get("HERMES_KANBAN_TASK"))
    root_task_id = _nonempty_string(marker.get("root_task_id"))
    search_id = _nonempty_string(marker.get("search_id"))
    idempotency_key = _nonempty_string(raw_input.get("idempotency_key"))
    marker_idempotency_key = _nonempty_string(marker.get("idempotency_key"))
    if not current_task_id or not root_task_id or root_task_id != marker.get("root_task_id"):
        return _search_block(
            "root_task_id must be explicit and match the current durable task context",
            assignee,
        )
    if not search_id or not idempotency_key or marker_idempotency_key != idempotency_key:
        return _search_block(
            "search_id and matching marker/tool idempotency_key are required for replay-safe admission",
            assignee,
        )
    conn: sqlite3.Connection | None = None
    try:
        conn = _search_connection(board)
        _search_require_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        _search_root_authorized(conn, root_task_id, current_task_id)
        events = _search_events(conn, root_task_id)
        admissions = [
            payload for row, payload in events
            if row["kind"] == _INVESTIGATION_SEARCH_ADMISSION_KIND
            and payload.get("search_id") == search_id
        ]
        if len(admissions) > 1:
            raise RuntimeError("investigation search admission ledger contains duplicate admissions")
        active_other = [
            payload for payload in _search_active_contexts(conn, current_task_id)
            if payload.get("search_id") != search_id
        ]
        if not admissions:
            if active_other:
                conn.rollback()
                return _search_block(
                    "another search_id is already active for the current root",
                    assignee,
                )
            _search_insert_event(
                conn,
                root_task_id,
                _INVESTIGATION_SEARCH_ADMISSION_KIND,
                {
                    "schema_id": _INVESTIGATION_SEARCH_SCHEMA,
                    "search_id": search_id,
                    "root_task_id": root_task_id,
                    "idempotency_key": idempotency_key,
                    "budget": dict(budget),
                    "state": "active",
                },
            )
            admissions = [{
                "schema_id": _INVESTIGATION_SEARCH_SCHEMA,
                "search_id": search_id,
                "root_task_id": root_task_id,
                "idempotency_key": idempotency_key,
                "budget": dict(budget),
                "state": "active",
            }]
        _search_validate_admission_payload(
            admissions[0], root_task_id=root_task_id, search_id=search_id, budget=budget,
        )

        candidate_events = [
            payload for row, payload in _search_events(
                conn, root_task_id, _INVESTIGATION_SEARCH_CANDIDATE_KIND
            )
            if payload.get("search_id") == search_id
        ]
        _search_validate_candidate_events(
            candidate_events, root_task_id=root_task_id, search_id=search_id, budget=budget,
        )
        selector_events = [
            payload for row, payload in _search_events(
                conn, root_task_id, _INVESTIGATION_SEARCH_SELECTOR_KIND
            )
            if payload.get("search_id") == search_id
        ]
        _search_validate_selector_events(
            selector_events, root_task_id=root_task_id, search_id=search_id,
        )
        closed_selector_events = [
            payload for payload in selector_events if payload.get("state") == "closed"
        ]
        if phase == "candidate":
            matching = [
                payload for payload in candidate_events
                if payload.get("candidate_id") == candidate_id
            ]
            if any(
                payload.get("idempotency_key") == idempotency_key
                and payload.get("candidate_id") != candidate_id
                for payload in candidate_events
            ):
                conn.rollback()
                return _search_block(
                    "the candidate idempotency_key is already bound to another candidate",
                    assignee,
                )
            if matching:
                if any(payload.get("idempotency_key") == idempotency_key for payload in matching):
                    conn.commit()
                    return 0
                conn.rollback()
                return _search_block(
                    f"candidate {candidate_id} already has a different idempotency key",
                    assignee,
                )
            if closed_selector_events:
                conn.rollback()
                return _search_block("the search was already closed by its selector", assignee)
            if len({payload.get("candidate_id") for payload in candidate_events}) >= _INVESTIGATION_SEARCH_MAX_CANDIDATES:
                conn.rollback()
                return _search_block("candidate admission would exceed the two-candidate cap", assignee)
            token_reservation = (
                int(budget["max_total_tokens"]) + _INVESTIGATION_SEARCH_MAX_CANDIDATES - 1
            ) // _INVESTIGATION_SEARCH_MAX_CANDIDATES
            retry_reservation = 1
            used_tokens = sum(
                int(payload.get("token_reservation", 0))
                for payload in candidate_events
                if isinstance(payload.get("token_reservation"), int)
            )
            used_retries = sum(
                int(payload.get("retry_reservation", 0))
                for payload in candidate_events
                if isinstance(payload.get("retry_reservation"), int)
            )
            if used_tokens + token_reservation > int(budget["max_total_tokens"]):
                conn.rollback()
                return _search_block("cumulative token admission is exhausted", assignee)
            if used_retries + retry_reservation > int(budget["max_retries"]):
                conn.rollback()
                return _search_block("cumulative retry admission is exhausted", assignee)
            _search_insert_event(
                conn,
                root_task_id,
                _INVESTIGATION_SEARCH_CANDIDATE_KIND,
                {
                    "schema_id": _INVESTIGATION_SEARCH_SCHEMA,
                    "search_id": search_id,
                    "root_task_id": root_task_id,
                    "candidate_id": candidate_id,
                    "idempotency_key": idempotency_key,
                    "token_reservation": token_reservation,
                    "retry_reservation": retry_reservation,
                },
            )
            conn.commit()
            return 0

        pending_selector = (
            marker.get("selection_status") in {"pending", "awaiting_expansion"}
            and marker.get("expansion_count", 0) == 0
        )
        single_selector = (
            marker.get("selection_status") in {"selected", "no_selection"}
            and marker.get("expansion_count", 0) == 0
        )
        candidate_task_ids = _string_list(marker.get("candidate_task_ids"))
        requested_selector_state = "pending" if pending_selector else "closed"
        matching_selectors = [
            payload for payload in selector_events
            if payload.get("idempotency_key") == idempotency_key
        ]
        if matching_selectors:
            if any(
                payload.get("state") != requested_selector_state
                or _string_list(payload.get("candidate_task_ids")) != candidate_task_ids
                for payload in matching_selectors
            ):
                conn.rollback()
                return _search_block(
                    "selector idempotency_key is already bound to a different selector admission",
                    assignee,
                )
            conn.commit()
            return 0
        if pending_selector and selector_events:
            conn.rollback()
            return _search_block("the search already has a different pending selector admission", assignee)
        if (
            selector_events
            and not pending_selector
            and not single_selector
            and marker.get("expansion_count") != _INVESTIGATION_SEARCH_MAX_EXPANSIONS
        ):
            conn.rollback()
            return _search_block(
                "a final selector after pending expansion must declare exactly one expansion",
                assignee,
            )
        if closed_selector_events:
            conn.rollback()
            return _search_block("the search already has a different selector admission", assignee)
        if marker.get("expansion_count") == _INVESTIGATION_SEARCH_MAX_EXPANSIONS and not selector_events:
            conn.rollback()
            return _search_block(
                "one expansion requires a preceding pending selector admission",
                assignee,
            )
        expected_candidate_ids = (
            {"A"} if pending_selector or single_selector else _INVESTIGATION_SEARCH_CANDIDATES
        )
        if {payload.get("candidate_id") for payload in candidate_events} != expected_candidate_ids:
            conn.rollback()
            return _search_block(
                "pending selector admission requires candidate A, final selector requires both A and B",
                assignee,
            )
        candidate_keys = {
            payload.get("idempotency_key") for payload in candidate_events
        }
        for task_id in candidate_task_ids:
            task = conn.execute(
                "SELECT assignee, idempotency_key FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task is None:
                conn.rollback()
                return _search_block(
                    f"selector candidate task {task_id} is not present on the durable board",
                    assignee,
                )
            if str(task["assignee"] or "").strip().casefold() != "kanban-investigator":
                conn.rollback()
                return _search_block(
                    f"selector candidate task {task_id} is not assigned to kanban-investigator",
                    assignee,
                )
            if task["idempotency_key"] not in candidate_keys:
                conn.rollback()
                return _search_block(
                    f"selector candidate task {task_id} is not bound to this search admission",
                    assignee,
                )
            if not _search_task_descends_from(conn, root_task_id, task_id):
                conn.rollback()
                return _search_block(
                    f"selector candidate task {task_id} is outside the search root",
                    assignee,
                )
        _search_insert_event(
            conn,
            root_task_id,
            _INVESTIGATION_SEARCH_SELECTOR_KIND,
            {
                "schema_id": _INVESTIGATION_SEARCH_SCHEMA,
                "search_id": search_id,
                "root_task_id": root_task_id,
                "candidate_task_ids": candidate_task_ids,
                "idempotency_key": idempotency_key,
                "state": "pending" if pending_selector else "closed",
            },
        )
        conn.commit()
        return 0
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return _search_block(
            "trusted durable admission is unavailable: " + f"{type(exc).__name__}: {exc}",
            assignee,
        )
    finally:
        if conn is not None:
            conn.close()


def _active_search_for_current(
    board: str, current_task_id: str | None = None,
) -> bool | None:
    current_task_id = current_task_id or _nonempty_string(os.environ.get("HERMES_KANBAN_TASK"))
    if not current_task_id:
        return False
    conn: sqlite3.Connection | None = None
    try:
        conn = _search_connection(board, read_only=True)
        _search_require_schema(conn)
        return bool(_search_active_contexts(conn, current_task_id))
    except (OSError, RuntimeError, sqlite3.Error):
        return None
    finally:
        if conn is not None:
            conn.close()


def _reject_unmarked_search_create(
    board: str, assignee: str, current_task_id: str | None = None,
) -> int:
    active = _active_search_for_current(board, current_task_id)
    if active is True:
        return _search_block(
            "an active search admission requires the same investigation_search marker on every creation surface",
            assignee,
        )
    if active is None and (current_task_id or _nonempty_string(os.environ.get("HERMES_KANBAN_TASK"))):
        return _search_block(
            "could not read the active search admission from the canonical board DB",
            assignee,
        )
    return 0


def _evaluate_investigation_search_create(
    raw_input: Mapping[str, Any], assignee: str, current_task_id: str | None = None,
) -> int:
    marker = raw_input.get("investigation_search")
    if not isinstance(marker, Mapping):
        return _search_block("investigation_search must be an object", assignee)
    schema = marker.get("schema_id") or marker.get("schema")
    search_id = marker.get("search_id")
    phase = marker.get("phase")
    if (
        schema != _INVESTIGATION_SEARCH_SCHEMA
        or not isinstance(search_id, str) or not search_id.strip()
        or phase not in _INVESTIGATION_SEARCH_PHASES
    ):
        return _search_block(
            "marker schema_id, search_id, and phase=candidate|selector are required",
            assignee,
        )
    if not _nonempty_string(raw_input.get("title")):
        return _search_block("every bounded search task requires a non-empty title", assignee)
    trigger_error = _search_trigger_error(marker)
    if trigger_error is not None:
        return _search_block(trigger_error, assignee)
    if phase == "candidate" and (
        assignee != "kanban-investigator"
        or marker.get("candidate_id") not in _INVESTIGATION_SEARCH_CANDIDATES
    ):
        return _search_block(
            "candidate_id must be A or B and the assignee must be kanban-investigator",
            assignee,
        )
    if phase == "selector":
        pending_selector = (
            marker.get("selection_status") in {"pending", "awaiting_expansion"}
            and marker.get("expansion_count", 0) == 0
        )
        single_selector = (
            marker.get("selection_status") in {"selected", "no_selection"}
            and marker.get("expansion_count", 0) == 0
        )
        expected_count = 1 if pending_selector or single_selector else _INVESTIGATION_SEARCH_MAX_CANDIDATES
        early_candidate_task_ids = _string_list(marker.get("candidate_task_ids"))
        early_parents = _string_list(raw_input.get("parents"))
        if len(early_candidate_task_ids) != expected_count or len(set(early_candidate_task_ids)) != expected_count:
            return _search_block(
                "the selector must name one candidate while pending or exactly two distinct candidate task IDs",
                assignee,
            )
        if sorted(early_candidate_task_ids) != sorted(early_parents):
            return _search_block(
                "selector parents must be exactly the declared candidate task IDs",
                assignee,
            )
    budget, budget_error = _search_budget(marker)
    if budget_error is not None or budget is None:
        return _search_block(budget_error or "the bounded budget is invalid", assignee)
    raw_runtime = raw_input.get("max_runtime_seconds")
    if not _positive_int(raw_runtime) or raw_runtime != budget["max_runtime_seconds"]:
        return _search_block(
            "max_runtime_seconds must be present and match the declared bounded budget",
            assignee,
        )
    raw_retries = raw_input.get("max_retries")
    if raw_retries != _KANBAN_TASK_RETRY_LIMIT:
        return _search_block(
            "task max_retries must be exactly five so timeout/protocol retry loops share the same safety limit",
            assignee,
        )
    expansion_count = marker.get("expansion_count", 0)
    if not isinstance(expansion_count, int) or isinstance(expansion_count, bool) or not 0 <= expansion_count <= _INVESTIGATION_SEARCH_MAX_EXPANSIONS:
        return _search_block("expansion_count must be 0 or 1", assignee)
    if not _nonempty_string(marker.get("root_task_id")):
        return _search_block("root_task_id is required for trusted admission", assignee)

    if phase == "candidate":
        candidate_id = marker.get("candidate_id")
        if assignee != "kanban-investigator" or candidate_id not in _INVESTIGATION_SEARCH_CANDIDATES:
            return _search_block(
                "candidate_id must be A or B and the assignee must be kanban-investigator",
                assignee,
            )
        if not _nonempty_string(raw_input.get("idempotency_key")):
            return _search_block("each candidate requires a stable idempotency_key", assignee)
        return _search_admit(
            raw_input, marker, assignee, budget, "candidate", candidate_id, current_task_id
        )

    if assignee != "kanban-main":
        return _search_block("the selector must be assigned to kanban-main", assignee)
    candidate_task_ids = _string_list(marker.get("candidate_task_ids"))
    parents = _string_list(raw_input.get("parents"))
    pending_selector = (
        marker.get("selection_status") in {"pending", "awaiting_expansion"}
        and marker.get("expansion_count", 0) == 0
    )
    single_selector = (
        marker.get("selection_status") in {"selected", "no_selection"}
        and marker.get("expansion_count", 0) == 0
    )
    expected_count = 1 if pending_selector or single_selector else _INVESTIGATION_SEARCH_MAX_CANDIDATES
    if len(candidate_task_ids) != expected_count or len(set(candidate_task_ids)) != expected_count:
        return _search_block(
            "the selector must name one candidate while pending or exactly two distinct candidate task IDs",
            assignee,
        )
    if sorted(candidate_task_ids) != sorted(parents):
        return _search_block(
            "selector parents must be exactly the declared candidate task IDs",
            assignee,
        )
    if not _nonempty_string(raw_input.get("idempotency_key")):
        return _search_block("the selector requires a stable idempotency_key", assignee)
    return _search_admit(
        raw_input, marker, assignee, budget, "selector", None, current_task_id
    )


def _evaluate_structured(payload: Mapping[str, Any]) -> int:
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, Mapping):
        return _block(
            "H4V3 specialist completion-contract gate failed closed: kanban_create "
            "tool_input must be an object. No task mutation was performed.",
            source="kanban_create",
        )
    board = str(raw_input.get("board") or "")
    try:
        current_task_id = _direct_worker_task_id(payload, board)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return _search_block(
            "could not prove dispatcher-owned worker identity: "
            f"{type(exc).__name__}: {exc}",
            str(raw_input.get("assignee") or ""),
        )
    assignee_value = str(raw_input.get("assignee") or "").strip().casefold()
    assignee = _specialist(raw_input.get("assignee"))
    if assignee is None:
        if assignee_value == "kanban-main":
            if "investigation_search" in raw_input:
                search_decision = _evaluate_investigation_search_create(
                    raw_input, "kanban-main", current_task_id
                )
                if search_decision != 0:
                    return search_decision
            else:
                search_decision = _reject_unmarked_search_create(
                    board, "kanban-main", current_task_id
                )
                if search_decision != 0:
                    return search_decision
        return 0
    if "investigation_search" in raw_input:
        search_decision = _evaluate_investigation_search_create(
            raw_input, assignee, current_task_id
        )
        if search_decision != 0:
            return search_decision
    else:
        search_decision = _reject_unmarked_search_create(board, assignee, current_task_id)
        if search_decision != 0:
            return search_decision
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


def _terminal_search_marker(args: list[str]) -> Mapping[str, Any] | None:
    for body in _option_values(args, "--body"):
        try:
            value = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping) and isinstance(value.get("investigation_search"), Mapping):
            return value["investigation_search"]
    return None


def _terminal_int_option(args: list[str], option: str) -> int | None:
    values = _option_values(args, option)
    if not values:
        return None
    if len(values) != 1:
        raise RuntimeError(f"{option} must be supplied exactly once")
    try:
        return int(values[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{option} must be an integer") from exc


def _terminal_runtime_option(args: list[str]) -> int | None:
    values = _option_values(args, "--max-runtime")
    if not values:
        return None
    if len(values) != 1:
        raise RuntimeError("--max-runtime must be supplied exactly once")
    raw = values[0].strip().lower()
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not raw or raw[-1] not in units:
        raise RuntimeError("--max-runtime must be seconds or a duration such as 15m")
    try:
        return int(float(raw[:-1]) * units[raw[-1]])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("--max-runtime must be seconds or a duration such as 15m") from exc


def _terminal_search_input(
    args: list[str], board: str, assignee: str, marker: Mapping[str, Any],
) -> dict[str, Any]:
    bodies = _option_values(args, "--body")
    body: dict[str, Any] = {}
    if bodies:
        try:
            decoded = json.loads(bodies[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {}
        if isinstance(decoded, Mapping):
            body.update(decoded)
    body["investigation_search"] = marker
    body["assignee"] = assignee
    body["board"] = board
    if args and not args[0].startswith("-"):
        body["title"] = args[0]
    body["parents"] = _option_values(args, "--parent")
    runtime = _terminal_runtime_option(args)
    retries = _terminal_int_option(args, "--max-retries")
    if runtime is not None:
        body["max_runtime_seconds"] = runtime
    if retries is not None:
        body["max_retries"] = retries
    keys = _option_values(args, "--idempotency-key")
    if keys:
        body["idempotency_key"] = keys[0]
    return body


def _evaluate_terminal_create(args: list[str], board: str) -> int:
    assignees = _option_values(args, "--assignee")
    assignee: str | None = None
    for value in assignees:
        normalized = value.strip().casefold()
        if normalized == "kanban-main":
            assignee = normalized
            break
        specialist = _specialist(value)
        if specialist is not None:
            assignee = specialist
            break
    if assignee is None:
        return 0
    _require_unique_terminal_options(args)
    marker = _terminal_search_marker(args)
    if marker is not None:
        raw_input = _terminal_search_input(args, board, assignee, marker)
        search_decision = _evaluate_investigation_search_create(raw_input, assignee)
        if search_decision != 0:
            return search_decision
    else:
        search_decision = _reject_unmarked_search_create(board, assignee)
        if search_decision != 0:
            return search_decision
    if assignee == "kanban-main":
        return 0
    parents = _option_values(args, "--parent")
    initial_statuses = _option_values(args, "--initial-status")
    if parents and any(_initial_status_is_blocked(value) for value in initial_statuses):
        return _block(
            _dependency_wait_diagnostic(assignee),
            assignee=assignee,
            source="terminal:create:dependency_wait",
        )
    contracts = _option_values(args, "--completion-contract")
    if not contracts or all(_contract_is_local(value) for value in contracts):
        return 0
    return _block(
        _diagnostic(assignee),
        assignee=assignee,
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
