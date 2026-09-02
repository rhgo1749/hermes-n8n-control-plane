#!/usr/bin/env python3
"""Fail-closed pre-tool gate for ambiguous ``kanban_block`` calls.

Hermes core intentionally keeps the legacy optional ``kind`` API for
compatibility.  This control-plane hook prevents a missing kind from silently
becoming a durable human-attention block.  It is read-only: it resolves and
queries the canonical Kanban SQLite database, then either allows the call or
returns the shell-hook block directive.

The hook is registered for both the MCP ``kanban_block`` tool and terminal
commands invoking ``hermes kanban block``.  Explicit canonical kinds are
allowed after the task/parent graph has been read successfully.  Missing,
empty, malformed, ambiguous, or unresolvable input fails closed.

Terminal boundary (Issue #92 rework round 3): the classifier first tries to
definitively parse a recognized invocation chain (env assignments, launcher
prefixes, a supported ``sh``/``bash``/``dash``/``zsh``/``ksh`` ``-c``/``-lc``
inline command, bounded depth).  When a command is *not* definitively parsed
but conservatively references the ``hermes kanban block`` execution family
(``hermes`` present as a lowercase substring, ``kanban`` and ``block`` as
tokens, in any position: quoted inline command, ``eval`` or variable
indirection, script-form argument, or compound segment), it fails closed
instead of guessing a ``kind`` or allowing the legacy ``kind=None`` mutation
path.  Commands that do not reference the family at all (for example
``printf hello`` or ``bash -lc 'printf hello'``) remain fail-open.  Shell
options that can reinterpret their arguments (for example ``bash -i -c``)
never prevent the family check, so dynamic spellings cannot bypass the gate.
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

VALID_BLOCK_KINDS = frozenset({"dependency", "needs_input", "capability", "transient"})
TERMINAL_PARENT_STATES = frozenset({"done", "archived"})
_LOG_PATH = Path(
    os.environ.get(
        "KANBAN_BLOCK_KIND_GUARD_LOG",
        "/home/hermes/.hermes/kanban/logs/block-kind-guard.log",
    )
)
_COMMAND_SEPARATORS = frozenset({";", "|", "&"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_BINARIES = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_LAUNCHER_BINARIES = frozenset({"command", "builtin", "exec", "nohup"})
_MAX_UNWRAP_DEPTH = 8


class GuardError(RuntimeError):
    """The call cannot be safely classified from durable board state."""


def _log(entry: Mapping[str, Any]) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), ensure_ascii=False) + "\n")
    except OSError:
        # The audit log is useful but must not turn a deterministic block into
        # a different failure mode.
        pass


def _block(message: str, *, task_id: str = "", board: str = "") -> int:
    print(json.dumps({"action": "block", "message": message}, ensure_ascii=False))
    _log(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "decision": "block",
            "task_id": task_id,
            "board": board,
            "message": message,
        }
    )
    return 2


def _homes() -> list[Path]:
    root = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    homes = [root]
    # Named profiles share the root Kanban registry.  Keep the configured
    # profile first, then the real default home as a compatibility fallback.
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
    """Resolve the canonical DB without creating or opening any fallback DB."""
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned:
        path = Path(pinned).expanduser()
        if path.is_file():
            return path
        raise GuardError(f"HERMES_KANBAN_DB does not point to a readable DB: {path}")

    requested = board.strip() or os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    for home in _homes():
        slug = requested or _current_slug(home) or "default"
        if slug == "default":
            candidate = home / "kanban.db"
        else:
            candidate = home / "kanban" / "boards" / slug / "kanban.db"
        if candidate.is_file():
            return candidate
    detail = requested or "default"
    raise GuardError(f"could not resolve board DB for board '{detail}'")


def _read_projection(db_path: Path, task_id: str) -> dict[str, Any]:
    if not task_id.strip():
        raise GuardError("task_id is required (or set HERMES_KANBAN_TASK in the environment)")

    uri = db_path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
    except sqlite3.Error as exc:
        raise GuardError(f"could not open board DB read-only: {type(exc).__name__}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('tasks', 'task_links')"
            )
        }
        missing_tables = {"tasks", "task_links"} - tables
        if missing_tables:
            raise GuardError(
                "board DB schema is incomplete; missing " + ", ".join(sorted(missing_tables))
            )
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
        if not {"id", "status", "block_kind"}.issubset(columns):
            raise GuardError("board DB tasks schema is incompatible with block-kind gate")
        task = conn.execute(
            "SELECT id, status, block_kind FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            raise GuardError(f"task '{task_id}' was not found on the resolved board")
        rows = conn.execute(
            "SELECT l.parent_id, t.status AS parent_status "
            "FROM task_links AS l LEFT JOIN tasks AS t ON t.id = l.parent_id "
            "WHERE l.child_id = ? ORDER BY l.parent_id",
            (task_id,),
        ).fetchall()
        parents: list[dict[str, str]] = []
        pending: list[dict[str, str]] = []
        for row in rows:
            parent_id = str(row["parent_id"])
            status = str(row["parent_status"] or "missing")
            parent = {"id": parent_id, "status": status}
            parents.append(parent)
            if status not in TERMINAL_PARENT_STATES:
                pending.append(parent)
        return {
            "task_id": task_id,
            "status": str(task["status"]),
            "block_kind": task["block_kind"],
            "parents": parents,
            "pending": pending,
        }
    except sqlite3.Error as exc:
        raise GuardError(f"could not read board DB: {type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()


def _diagnostic(task_id: str, projection: Mapping[str, Any]) -> str:
    pending = list(projection.get("pending") or [])
    if pending:
        rendered = ", ".join(
            f"{item['id']}({item['status']})"
            for item in pending
            if isinstance(item, Mapping)
        )
        return (
            f"kanban_block for {task_id} requires an explicit kind; unresolved dependencies: "
            f"{rendered} — use explicit kind=dependency. No task mutation was performed."
        )
    return (
        f"kanban_block for {task_id} requires an explicit kind; pick an explicit kind: "
        "dependency/needs_input/capability/transient. No task mutation was performed."
    )


def _kind_from_value(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise GuardError("kind must be a string")
    return value.strip()


def _extract_tool_call(payload: Mapping[str, Any]) -> tuple[str | None, str, str, str]:
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, Mapping):
        raise GuardError("kanban_block tool input must be an object")
    extra = payload.get("extra")
    extra_map = extra if isinstance(extra, Mapping) else {}
    task_id = str(
        raw_input.get("task_id")
        or extra_map.get("task_id")
        or os.environ.get("HERMES_KANBAN_TASK", "")
    ).strip()
    board = str(
        raw_input.get("board")
        or extra_map.get("board")
        or os.environ.get("HERMES_KANBAN_BOARD", "")
    ).strip()
    kind = _kind_from_value(raw_input.get("kind"))
    return kind, task_id, board, "kanban_block"


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError as exc:
        raise GuardError(f"could not parse hermes kanban command: {exc}") from exc


# Conservative execution-family marker.  ``hermes`` and ``kanban`` must appear
# as whole words in order, and ``block`` must appear as a whole word *after*
# them.  Requiring ``block`` as a whole word means ``unblock`` and ``show``
# (a different word) do not fire, while variable indirection (``x=hermes; $x
# kanban block ...``) and a quoted inline command (``eval "hermes kanban
# block ..."``) still reference the family.
_BLOCK_FAMILY_RE = re.compile(
    r"\bhermes\b[\s\S]*?\bkanban\b[\s\S]*?\bblock\b",
    re.IGNORECASE,
)


def _references_block_family(command: str) -> bool:
    """Conservative marker for the ``hermes kanban block`` execution family.

    Fires when an executable command string references the family: a top-level
    invocation, an inline ``-c`` command, ``eval`` or variable indirection, or
    a compound segment.  It intentionally does *not* fire on ``hermes kanban
    unblock`` / ``show`` (a different word than ``block``) or on a command that
    has no ``hermes`` reference at all.  A reference the parser could not
    definitively classify is the *safe* case to fail closed on, so it is treated
    as a potential ``hermes kanban block`` invocation.
    """
    return _BLOCK_FAMILY_RE.search(command) is not None


def _consume_env_prefix(tokens: list[str], index: int) -> int | None:
    """Consume a conservative, invocation-only subset of ``env`` options."""
    index += 1
    while index < len(tokens):
        value = tokens[index]
        if value == "--":
            return index + 1
        if _ENV_ASSIGN_RE.fullmatch(value):
            index += 1
            continue
        if value in ("-i", "--ignore-environment", "-0", "--null"):
            index += 1
            continue
        if value in ("-u", "--unset", "-C", "--chdir"):
            if index + 1 >= len(tokens):
                return None
            index += 2
            continue
        if value.startswith(("--unset=", "--chdir=")):
            if value.partition("=")[2] == "":
                return None
            index += 1
            continue
        if value.startswith("-u") and len(value) > 2:
            index += 1
            continue
        if value.startswith("-C") and len(value) > 2:
            index += 1
            continue
        if not value.startswith("-"):
            return index
        # ``env -S`` and unknown options can reinterpret the remaining tokens;
        # do not guess at their command shape.
        return None
    return index


def _consume_launcher_prefix(tokens: list[str], index: int) -> int | None:
    """Consume one supported launcher and return the next executable index."""
    name = Path(tokens[index]).name
    if name == "env":
        return _consume_env_prefix(tokens, index)

    index += 1
    if name == "command":
        while index < len(tokens):
            value = tokens[index]
            if value == "--":
                return index + 1
            if value == "-p":
                index += 1
                continue
            if value.startswith("-"):
                return None
            return index
        return index

    if name == "builtin":
        if index < len(tokens) and tokens[index] == "--":
            index += 1
        return index if index < len(tokens) and not tokens[index].startswith("-") else None

    if name == "exec":
        while index < len(tokens):
            value = tokens[index]
            if value == "--":
                return index + 1
            if value in ("-c", "-l", "-cl", "-lc"):
                index += 1
                continue
            if value == "-a":
                if index + 1 >= len(tokens):
                    return None
                index += 2
                continue
            if value.startswith("-a") and len(value) > 2:
                index += 1
                continue
            if value.startswith("-"):
                return None
            return index
        return index

    if name == "nohup":
        if index < len(tokens) and tokens[index] == "--":
            index += 1
        if index >= len(tokens) or tokens[index].startswith("-"):
            return None
        return index

    return None


def _launcher_shell_index(tokens: list[str]) -> int | None:
    """Return the shell index after safe assignments and launcher prefixes."""
    index = 0
    while index < len(tokens) and _ENV_ASSIGN_RE.fullmatch(tokens[index]):
        index += 1
    while index < len(tokens):
        name = Path(tokens[index]).name
        if name == "env" or name in _LAUNCHER_BINARIES:
            next_index = _consume_launcher_prefix(tokens, index)
            if next_index is None or next_index == index:
                return None
            index = next_index
            continue
        return index
    return None


_SHELL_FLAG_ONLY = frozenset(
    {
        # Flags that do not take a command string; they may appear (grouped or
        # not) before a ``-c`` and never change which argument is the command.
        "-l", "--login", "-i", "--interactive", "-e", "-x", "-n", "--norc",
        "--noprofile", "--posix", "-v", "-s", "-p", "-f",
    }
)


def _shell_inline_command(tokens: list[str], index: int) -> str | None:
    """Return the command string a recognized shell will execute.

    Scans the option cluster after the shell name.  A *command-string* option
    (``-c``, ``--command``, or a grouped short option whose characters include
    ``c``, e.g. ``-lc``/``-ilc``/``-elc``) makes the following token the
    command to execute; flag-only options (``-i``, ``-e``, ``-l``, ``-x``,
    ``--login``, ...) are skipped.  A command string is only returned when such
    an option is present, so a shell invoked with a bare script or a positional
    argument (for example ``dash -x '...'`` or ``/tmp/w.sh '...'``) is *not*
    treated as an inline command and keeps the fail-open behavior.
    """
    rest = tokens[index + 1 :]
    position = 0
    while position < len(rest):
        value = rest[position]
        if value == "--command" or value == "-c":
            if position + 1 >= len(rest):
                return None
            return rest[position + 1]
        if value in _SHELL_FLAG_ONLY:
            position += 1
            continue
        if (
            value.startswith("-")
            and not value.startswith("--")
            and len(value) > 1
            and "c" in value[1:]
        ):
            # Grouped short option containing ``c``: the next token is the
            # command string (bash ``-ilc`` == ``-i -l -c``).
            if position + 1 >= len(rest):
                return None
            return rest[position + 1]
        # A long option we do not classify, or a non-dash token (a script path
        # / positional argument): the shell will not run an inline command
        # string here, so leave it fail-open rather than guess.
        return None
    return None


def _unwrap_shell_command(tokens: list[str]) -> str | None:
    """Return a definitively parsed supported shell ``-c`` command.

    Only recognized invocation chains (env assignments, ``env``/``command``/
    ``builtin``/``exec``/``nohup`` prefixes, supported shell option spellings)
    yield a parsed inner command.  Any other shape returns ``None``; the caller
    then classifies the conservative block family instead of guessing an inner
    command.  This never infers or repairs a ``kind``.
    """
    index = _launcher_shell_index(tokens)
    if index is None or index >= len(tokens):
        return None
    if Path(tokens[index]).name not in _SHELL_BINARIES:
        return None
    return _shell_inline_command(tokens, index)


def _terminal_call(
    command: str, depth: int = 0
) -> tuple[str | None, str, str, str] | None:
    """Return one parsed block command, or None for unrelated terminal input.

    A definitively parsed inline shell wrapper (``sh -c``, ``bash -lc``, ...)
    is unwrapped first so a ``hermes kanban block`` call hidden behind a
    recognized shell cannot bypass the gate.  A top-level ``hermes`` invocation
    is classified directly.  If none of that resolves but the command still
    references the ``hermes kanban block`` execution family (an unrecognized
    shell option, ``eval``, variable indirection, or an indirect form), it fails
    closed (``GuardError``) rather than allowing the legacy ``kind=None``
    mutation path; only commands that do not reference the family remain
    fail-open.
    """
    tokens = _split_command(command)
    inner = _unwrap_shell_command(tokens)
    if inner is not None:
        if depth >= _MAX_UNWRAP_DEPTH:
            raise GuardError(
                "supported shell wrapper nesting exceeds the maximum "
                f"depth of {_MAX_UNWRAP_DEPTH}; command classification failed closed"
            )
        return _terminal_call(inner, depth + 1)
    for start, token in enumerate(tokens):
        if Path(token).name != "hermes":
            continue
        end = start + 1
        while end < len(tokens) and tokens[end] not in _COMMAND_SEPARATORS:
            end += 1
        segment = tokens[start + 1 : end]
        try:
            kanban_at = segment.index("kanban")
        except ValueError:
            continue
        subcommand = segment[kanban_at + 1 :]
        if "block" not in subcommand:
            continue
        block_at = subcommand.index("block")
        board = ""
        prefix = subcommand[:block_at]
        for index, value in enumerate(prefix):
            if value == "--board" and index + 1 < len(prefix):
                board = prefix[index + 1]
            elif value.startswith("--board="):
                board = value.split("=", 1)[1]
        args = subcommand[block_at + 1 :]
        kind: str | None = None
        positionals: list[str] = []
        index = 0
        while index < len(args):
            value = args[index]
            if value == "--board" and index + 1 < len(args):
                board = args[index + 1]
                index += 2
                continue
            if value.startswith("--board="):
                board = value.split("=", 1)[1]
                index += 1
                continue
            if value == "--kind" and index + 1 < len(args):
                kind = _kind_from_value(args[index + 1])
                index += 2
                continue
            if value.startswith("--kind="):
                kind = _kind_from_value(value.split("=", 1)[1])
                index += 1
                continue
            if not value.startswith("-"):
                positionals.append(value)
            index += 1
        task_id = positionals[0] if positionals else os.environ.get("HERMES_KANBAN_TASK", "")
        return kind, task_id.strip(), board.strip(), "terminal"
    if _references_block_family(command):
        raise GuardError(
            "terminal command references a hermes kanban block invocation that "
            "the gate cannot classify definitively (unknown shell option, "
            "dynamic or indirect form). An explicit --kind is required before "
            "the command can be allowed; no task mutation was performed"
        )
    return None


def evaluate(kind: str | None, task_id: str, board: str, source: str) -> int:
    """Apply the fail-closed policy after reading the canonical board state."""
    db_path = _board_db_path(board)
    projection = _read_projection(db_path, task_id)
    if kind not in VALID_BLOCK_KINDS:
        return _block(_diagnostic(task_id, projection), task_id=task_id, board=board)
    return 0


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        # A configured fail-closed hook must not silently allow an event it
        # cannot parse.  The caller's matcher has already scoped this process
        # to the kanban_block/terminal pre-tool hooks.
        return _block(
            "kanban block-kind gate failed closed on malformed pre_tool_call "
            f"payload: {type(exc).__name__}: {exc}. No task mutation was performed."
        )
    if not isinstance(payload, Mapping):
        return 0

    tool_name = str(payload.get("tool_name") or "")
    try:
        if tool_name == "kanban_block":
            kind, task_id, board, source = _extract_tool_call(payload)
        elif tool_name == "terminal":
            raw_input = payload.get("tool_input")
            if not isinstance(raw_input, Mapping):
                return 0
            parsed = _terminal_call(str(raw_input.get("command") or ""))
            if parsed is None:
                return 0
            kind, task_id, board, source = parsed
        else:
            return 0
        return evaluate(kind, task_id, board, source)
    except GuardError as exc:
        task_id = str(payload.get("extra", {}).get("task_id") or "") if isinstance(payload.get("extra"), Mapping) else ""
        return _block(
            f"kanban block-kind gate failed closed: {exc}. No task mutation was performed.",
            task_id=task_id,
        )
    except (OSError, sqlite3.Error) as exc:
        return _block(
            "kanban block-kind gate failed closed while reading the board: "
            f"{type(exc).__name__}: {exc}. No task mutation was performed."
        )


if __name__ == "__main__":
    raise SystemExit(main())
