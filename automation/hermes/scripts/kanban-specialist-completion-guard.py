#!/usr/bin/env python3
"""Fail-closed H4V3 guard for specialist Kanban completion contracts.

Hermes core supports PR-aware ``completion_contract`` values because some
standalone Kanban tasks are terminal only after exact-head GitHub acceptance.
H4V3 specialist tasks have a different lifecycle boundary: Developer,
Reviewer, and Designer own bounded internal work and must be able to finish
while the linked PR is still open.  GitHub merge/review state is projected by
the canonical edge on the Issue-backed root card.

This pre-tool hook therefore rejects non-local completion contracts when a new
task is assigned to an H4V3 specialist profile.  Omitted
``completion_contract`` is safe because Hermes normalizes it to ``local-only``.
PR URLs, repository names, and head SHAs remain valid task-body / handoff
provenance; they are not specialist terminal policy.

The structured ``kanban_create`` tool is the canonical path.  The ``terminal``
matcher also closes the ordinary literal ``hermes kanban create`` bypass for
specialist assignees, including shell-wrapped command strings.  Unrelated
terminal commands and non-specialist PR-aware tasks remain untouched.
"""
from __future__ import annotations

import json
import os
import re
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
_SPECIALIST_TOKEN_RE = re.compile(
    r"\bkanban-(?:developer|reviewer|designer)\b",
    re.IGNORECASE,
)
_COMPLETION_FLAG_RE = re.compile(r"--completion-contract(?:=|\s+)", re.IGNORECASE)
_LOCAL_ONLY_FLAG_RE = re.compile(
    r"--completion-contract(?:=|\s+)[\"']?local-only[\"']?(?=\s|[\"']|$)",
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


def _diagnostic(assignee: str) -> str:
    return (
        f"H4V3 specialist task '{assignee}' must use completion_contract=local-only "
        "(or omit the field, which defaults to local-only). Developer/Reviewer/Designer "
        "done is an internal specialist terminal state; GitHub PR acceptance/merge is "
        "owned by root-card edge reconciliation. Keep PR URL/head/repository as body or "
        "completion metadata evidence instead. Retry the create call with local-only. "
        "No task mutation was performed."
    )


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


def _evaluate_terminal(payload: Mapping[str, Any]) -> int:
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, Mapping):
        return 0
    command = str(raw_input.get("command") or "")
    if not _CREATE_FAMILY_RE.search(command):
        return 0
    specialist_match = _SPECIALIST_TOKEN_RE.search(command)
    if specialist_match is None:
        return 0
    assignee = specialist_match.group(0).casefold()
    if not _COMPLETION_FLAG_RE.search(command):
        return 0
    if _LOCAL_ONLY_FLAG_RE.search(command):
        return 0
    return _block(_diagnostic(assignee), assignee=assignee, source="terminal")


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

    tool_name = str(payload.get("tool_name") or "")
    if tool_name == "kanban_create":
        return _evaluate_structured(payload)
    if tool_name == "terminal":
        return _evaluate_terminal(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
