#!/usr/bin/env python3
"""Fail-closed H4V3 guard for specialist Kanban completion contracts.

Hermes core supports PR-aware ``completion_contract`` values because some
standalone Kanban tasks are terminal only after exact-head GitHub acceptance.
H4V3 specialist tasks have a different lifecycle boundary: Developer,
Reviewer, and Designer own bounded internal work and must be able to finish
while the linked PR is still open. GitHub merge/review state is projected by
the canonical edge on the Issue-backed root card.

This pre-tool policy therefore rejects non-local completion contracts when a
new task is assigned to an H4V3 specialist profile. Omitted
``completion_contract`` is safe because Hermes normalizes it to ``local-only``.
PR URLs, repository names, and head SHAs remain valid task-body / handoff
provenance; they are not specialist terminal policy.

The structured ``kanban_create`` tool is the canonical creation path. The
``terminal`` policy closes ordinary literal/shell-wrapped ``hermes kanban
create`` bypasses and also rejects ``assign``/``reassign`` attempts that would
move an already PR-aware task to a specialist. Reassignment reads only the
canonical task row; unreadable/ambiguous state fails closed and is never
rewritten by this guard.

``evaluate_payload`` is intentionally importable by the already-approved
lifecycle hook wrapper so this policy does not need a second shell-hook command
or a second child Python process.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SPECIALIST_ASSIGNEES = frozenset(
    {"kanban-developer", "kanban-reviewer", "kanban-designer"}
)
LOCAL_ONLY = "local-only"
_LOG_PATH = Path(
    os.environ.get(
        "KANBAN_SPECIALIST_COMPLETION_GUARD_LOG",
        "/home/hermes/.hermes/kanban/logs/specialist-completion-guard.log",
    )
)
_CREATE_FAMILY_RE = re.compile(
    r"\bhermes\b[\s\S]*?\bkanban\b[\s\S]*?\bcreate\b",
    re.IGNORECASE,
)
_ASSIGNEE_RE = re.compile(
    r"--assignee(?:=|\s+)[\"']?(kanban-(?:developer|reviewer|designer))[\"']?"
    r"(?=\s|[\"']|$)",
    re.IGNORECASE,
)
_COMPLETION_VALUE_RE = re.compile(
    r"--completion-contract(?:=|\s+)[\"']?([^\s\"']+)[\"']?",
    re.IGNORECASE,
)
_COMPLETION_FLAG_RE = re.compile(r"--completion-contract(?:=|\s+)", re.IGNORECASE)
_ASSIGN_RE = re.compile(
    r"\bhermes\b[\s\S]*?\bkanban\b"
    r"(?P<prefix>[\s\S]*?)\b(?P<verb>assign|reassign)\b\s+"
    r"[\"']?(?P<task>[A-Za-z0-9_.:-]+)[\"']?\s+"
    r"[\"']?(?P<profile>kanban-(?:developer|reviewer|designer))[\"']?"
    r"(?=\s|[\"']|$)",
    re.IGNORECASE,
)
_BOARD_RE = re.compile(
    r"(?:^|\s)--board(?:=|\s+)[\"']?(?P<board>[A-Za-z0-9._-]+)[\"']?(?=\s|[\"']|$)",
    re.IGNORECASE,
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


def _diagnostic(assignee: str, *, action: str = "create") -> str:
    retry = (
        "Retry the create call with local-only."
        if action == "create"
        else "Keep the task on a non-specialist profile or recover its completion contract through an explicit operator procedure before reassignment."
    )
    return (
        f"H4V3 specialist task '{assignee}' must use completion_contract=local-only "
        "(or omit the field at creation, which defaults to local-only). "
        "Developer/Reviewer/Designer done is an internal specialist terminal state; "
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
    contract = raw_input.get("completion_contract")
    if _contract_is_local(contract):
        return 0
    return _block(_diagnostic(assignee), assignee=assignee, source="kanban_create")


def _evaluate_terminal_create(command: str) -> int | None:
    if not _CREATE_FAMILY_RE.search(command):
        return None
    assignee_match = _ASSIGNEE_RE.search(command)
    if assignee_match is None:
        return None
    assignee = assignee_match.group(1).casefold()
    if not _COMPLETION_FLAG_RE.search(command):
        return 0
    contracts = [value.strip() for value in _COMPLETION_VALUE_RE.findall(command)]
    if contracts and all(value == LOCAL_ONLY for value in contracts):
        return 0
    return _block(_diagnostic(assignee), assignee=assignee, source="terminal:create")


def _evaluate_terminal_assignment(command: str) -> int | None:
    match = _ASSIGN_RE.search(command)
    if match is None:
        return None
    task_id = match.group("task").strip()
    assignee = match.group("profile").casefold()
    prefix = match.group("prefix") or ""
    board_match = _BOARD_RE.search(prefix)
    board = board_match.group("board") if board_match is not None else ""
    try:
        contract = _task_completion_contract(task_id, board)
    except (OSError, RuntimeError) as exc:
        return _block(
            "H4V3 specialist completion-contract gate failed closed while validating "
            f"{match.group('verb').casefold()} of {task_id}: {type(exc).__name__}: {exc}. "
            "No task mutation was performed.",
            assignee=assignee,
            source=f"terminal:{match.group('verb').casefold()}",
        )
    if _contract_is_local(contract):
        return 0
    return _block(
        _diagnostic(assignee, action="assign"),
        assignee=assignee,
        source=f"terminal:{match.group('verb').casefold()}",
    )


def _evaluate_terminal(payload: Mapping[str, Any]) -> int:
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, Mapping):
        return 0
    command = str(raw_input.get("command") or "")
    create = _evaluate_terminal_create(command)
    if create is not None:
        return create
    assignment = _evaluate_terminal_assignment(command)
    return 0 if assignment is None else assignment


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
