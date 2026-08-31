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
"""
from __future__ import annotations

import json
import os
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


def _terminal_call(command: str) -> tuple[str | None, str, str, str] | None:
    """Return one parsed block command, or None for unrelated terminal input."""
    tokens = _split_command(command)
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
