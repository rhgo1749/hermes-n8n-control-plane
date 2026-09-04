"""Dispatcher-side safety wake for stranded GitHub-backed completions.

Primary path:
    worker kanban_complete -> github-completion-edge-wake -> canonical edge

This companion plugin is a liveness backstop only. It observes the existing
dispatcher tick and performs no GitHub polling on its own. When a recently
completed GitHub-backed task is still durably ``done`` at a later dispatcher
tick, it replays the already-installed primary completion observer for that
exact task. The canonical edge remains the only component allowed to project
GitHub state back into Kanban.

The backstop is deliberately bounded:
- only recent committed ``completed`` task events are inspected;
- only tasks still in provisional ``done`` are candidates;
- ordinary/non-GitHub cards are ignored;
- each completion event receives at most two replay attempts per process;
- a successful projection suppresses further attempts;
- no cron, sleep loop, external polling, second state store, or direct Kanban
  status write is introduced.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import sqlite3
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote

_LOG = logging.getLogger(__name__)

_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8,64}$")
_PROVENANCE_END = "## Canonical Issue body"
_SOURCE_LINE = re.compile(r"^\s*-\s*source\s*:\s*github-issue\s*$", re.IGNORECASE)
_COMPLETION_LINE = re.compile(
    r"^\s*-\s*completion contract\s*:\s*github-pr\s*$", re.IGNORECASE
)

_LOOKBACK_SECONDS = max(
    60,
    min(
        int(os.environ.get("HERMES_COMPLETION_SAFETY_LOOKBACK_SECONDS", "900")),
        3600,
    ),
)
_MAX_ATTEMPTS_PER_EVENT = 2
_MAX_TRACKED_EVENTS = 512

_ATTEMPTS: "OrderedDict[tuple[str, int], int]" = OrderedDict()
_PRIMARY_MODULE: Any | None = None


class _SafetyWakeFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _diagnostic(board: str | None, task_id: str | None, code: str) -> None:
    safe_board = board if isinstance(board, str) and _BOARD_RE.fullmatch(board) else "<invalid>"
    safe_task = (
        task_id
        if isinstance(task_id, str) and _TASK_ID_RE.fullmatch(task_id)
        else "<invalid>"
    )
    _LOG.warning(
        "GitHub completion dispatch safety wake skipped or failed: "
        "board=%s task=%s code=%s",
        safe_board,
        safe_task,
        code,
    )


def _valid_board(board: Any) -> str:
    if not isinstance(board, str) or not _BOARD_RE.fullmatch(board):
        raise _SafetyWakeFailure("invalid_board")
    pinned = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if pinned and pinned != board:
        raise _SafetyWakeFailure("board_pin_mismatch")
    return board


def _board_db_path(board: str) -> Path:
    try:
        from hermes_cli import kanban_db

        if not kanban_db.board_exists(board):
            raise _SafetyWakeFailure("board_missing")
        path = Path(kanban_db.kanban_db_path(board=board)).expanduser()
    except _SafetyWakeFailure:
        raise
    except Exception as exc:
        raise _SafetyWakeFailure("board_path_invalid") from exc

    if path.is_symlink():
        raise _SafetyWakeFailure("board_db_symlink")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _SafetyWakeFailure("board_db_missing") from exc
    if not resolved.is_file():
        raise _SafetyWakeFailure("board_db_not_file")
    return resolved


def _is_github_backed_body(body: Any) -> bool:
    provenance = str(body or "").split(_PROVENANCE_END, 1)[0]
    lines = provenance.splitlines()
    return any(_SOURCE_LINE.fullmatch(line) for line in lines) or any(
        _COMPLETION_LINE.fullmatch(line) for line in lines
    )


def _recent_stranded_completions(
    board: str,
    *,
    now: int | None = None,
) -> list[tuple[int, str]]:
    """Return recent completed events whose task is still provisional DONE."""
    db_path = _board_db_path(board)
    uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
    cutoff = int(time.time() if now is None else now) - _LOOKBACK_SECONDS

    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                    e.id AS event_id,
                    e.task_id AS task_id,
                    e.created_at AS created_at,
                    t.body AS body
                FROM task_events AS e
                JOIN tasks AS t ON t.id = e.task_id
                WHERE e.kind = 'completed'
                  AND e.created_at >= ?
                  AND t.status = 'done'
                ORDER BY e.id ASC
                LIMIT 64
                """,
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise _SafetyWakeFailure("board_read_failed") from exc

    result: list[tuple[int, str]] = []
    for row in rows:
        task_id = str(row["task_id"] or "")
        if not _TASK_ID_RE.fullmatch(task_id):
            continue
        if not _is_github_backed_body(row["body"]):
            continue
        result.append((int(row["event_id"]), task_id))
    return result


def _primary_plugin_path() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        root = Path(get_default_hermes_root()).expanduser().resolve(strict=True)
    except Exception as exc:
        raise _SafetyWakeFailure("runtime_home_unavailable") from exc

    candidate = root / "plugins" / "github-completion-edge-wake" / "__init__.py"
    if candidate.is_symlink():
        raise _SafetyWakeFailure("primary_plugin_symlink")
    try:
        resolved = candidate.resolve(strict=True)
        plugins_root = (root / "plugins").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _SafetyWakeFailure("primary_plugin_missing") from exc

    expected_parent = plugins_root / "github-completion-edge-wake"
    if resolved.parent != expected_parent or resolved.name != "__init__.py":
        raise _SafetyWakeFailure("primary_plugin_outside_runtime")
    if not resolved.is_file():
        raise _SafetyWakeFailure("primary_plugin_invalid")
    return resolved


def _load_primary() -> Any:
    global _PRIMARY_MODULE
    if _PRIMARY_MODULE is not None:
        return _PRIMARY_MODULE

    path = _primary_plugin_path()
    spec = importlib.util.spec_from_file_location(
        "github_completion_edge_wake_primary_for_dispatch_safety",
        path,
    )
    if spec is None or spec.loader is None:
        raise _SafetyWakeFailure("primary_plugin_load_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(spec.name, None)
        raise _SafetyWakeFailure("primary_plugin_load_failed") from exc

    for name in ("_completion_is_eligible", "_on_task_completed"):
        if not callable(getattr(module, name, None)):
            raise _SafetyWakeFailure("primary_plugin_contract_missing")
    _PRIMARY_MODULE = module
    return module


def _attempt_count(key: tuple[str, int]) -> int:
    return int(_ATTEMPTS.get(key, 0))


def _record_attempt(key: tuple[str, int]) -> None:
    _ATTEMPTS[key] = _attempt_count(key) + 1
    _ATTEMPTS.move_to_end(key)
    while len(_ATTEMPTS) > _MAX_TRACKED_EVENTS:
        _ATTEMPTS.popitem(last=False)


def _forget_event(key: tuple[str, int]) -> None:
    _ATTEMPTS.pop(key, None)


def _on_dispatch_tick(
    *,
    board: str | None = None,
    **_: Any,
) -> None:
    """Replay the primary completion wake for a recent stranded completion."""
    task_for_diag: str | None = None
    try:
        safe_board = _valid_board(board)
        candidates = _recent_stranded_completions(safe_board)
        if not candidates:
            return

        primary = _load_primary()
        selected: tuple[int, str] | None = None
        for event_id, task_id in candidates:
            key = (safe_board, event_id)
            if _attempt_count(key) >= _MAX_ATTEMPTS_PER_EVENT:
                continue
            selected = (event_id, task_id)
            break
        if selected is None:
            return

        event_id, task_id = selected
        task_for_diag = task_id
        key = (safe_board, event_id)
        _record_attempt(key)

        if not primary._completion_is_eligible(task_id, safe_board):
            _forget_event(key)
            return

        primary._on_task_completed(task_id=task_id, board=safe_board)

        for candidate_event_id, candidate_task_id in candidates:
            candidate_key = (safe_board, candidate_event_id)
            try:
                still_eligible = primary._completion_is_eligible(
                    candidate_task_id,
                    safe_board,
                )
            except Exception:
                continue
            if not still_eligible:
                _forget_event(candidate_key)

        if primary._completion_is_eligible(task_id, safe_board):
            _diagnostic(safe_board, task_id, "still_done_after_replay")
    except _SafetyWakeFailure as exc:
        _diagnostic(board if isinstance(board, str) else None, task_for_diag, exc.code)
    except Exception as exc:
        _diagnostic(
            board if isinstance(board, str) else None,
            task_for_diag,
            type(exc).__name__,
        )


def register(ctx: Any) -> None:
    ctx.register_hook("on_kanban_dispatch_tick", _on_dispatch_tick)


__all__ = [
    "register",
    "_is_github_backed_body",
    "_recent_stranded_completions",
    "_on_dispatch_tick",
]
