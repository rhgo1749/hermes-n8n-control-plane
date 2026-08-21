"""Completion-side wake for the deployed GitHub/Kanban edge reconciler.

This is an observer only. Hermes core commits the task completion first; this
plugin then reads the committed row and, only for GitHub-backed cards, invokes
the already-deployed edge owner once. The edge remains the only component that
projects GitHub state back into Kanban.
"""
# ruff: noqa: N999
from __future__ import annotations

import importlib
import logging
import os
import re
import selectors
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
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
_EDGE_SCRIPT = Path("scripts") / "kanban-github-sync.py"
_EDGE_TIMEOUT_SECONDS = 20.0
_EDGE_OUTPUT_LIMIT_BYTES = 64 * 1024


@dataclass(frozen=True)
class WakeResult:
    """Bounded result of one edge subprocess invocation."""

    returncode: int | None
    output_bytes: int
    timed_out: bool = False
    output_limited: bool = False


class _WakeFailure(RuntimeError):
    """Internal fail-closed diagnostic with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _valid_board(board: Any) -> str:
    if not isinstance(board, str) or not _BOARD_RE.fullmatch(board):
        raise _WakeFailure("invalid_board")

    pinned_board = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if pinned_board and pinned_board != board:
        raise _WakeFailure("board_pin_mismatch")
    if os.environ.get("HERMES_KANBAN_DB", "").strip() and not pinned_board:
        raise _WakeFailure("ambiguous_board_pin")
    return board


def _runtime_root() -> Path:
    """Resolve the shared Hermes root used by the live edge deployment."""
    try:
        hermes_constants = importlib.import_module("hermes_constants")
        root = hermes_constants.get_default_hermes_root()
    except Exception as exc:  # pragma: no cover - only old runtimes
        raw_home = os.environ.get("HERMES_HOME", "").strip()
        if not raw_home:
            raise _WakeFailure("runtime_home_missing") from exc
        root = Path(raw_home).expanduser()

    try:
        resolved = root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _WakeFailure("runtime_home_unavailable") from exc
    if not resolved.is_dir():
        raise _WakeFailure("runtime_home_not_directory")
    return resolved


def _edge_script_path() -> Path:
    root = _runtime_root()
    candidate = root / _EDGE_SCRIPT
    if candidate.is_symlink():
        raise _WakeFailure("edge_script_symlink")
    try:
        resolved = candidate.resolve(strict=True)
        scripts_root = (root / "scripts").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _WakeFailure("edge_script_missing") from exc
    if resolved.name != "kanban-github-sync.py" or not resolved.is_file():
        raise _WakeFailure("edge_script_invalid")
    if resolved.parent != scripts_root:
        raise _WakeFailure("edge_script_outside_runtime")
    return resolved


def _board_db_path(board: str) -> Path:
    try:
        kanban_db = importlib.import_module("hermes_cli.kanban_db")

        if not kanban_db.board_exists(board):
            raise _WakeFailure("board_missing")
        path = Path(kanban_db.kanban_db_path(board=board)).expanduser()
    except _WakeFailure:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _WakeFailure("board_path_invalid") from exc

    if path.is_symlink():
        raise _WakeFailure("board_db_symlink")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _WakeFailure("board_db_missing") from exc
    if not resolved.is_file():
        raise _WakeFailure("board_db_not_file")
    return resolved


def _is_github_backed_body(body: Any) -> bool:
    """Check only importer-owned provenance, never untrusted Issue prose."""
    provenance = str(body or "").split(_PROVENANCE_END, 1)[0]
    lines = provenance.splitlines()
    return any(_SOURCE_LINE.fullmatch(line) for line in lines) and any(
        _COMPLETION_LINE.fullmatch(line) for line in lines
    )


def _completion_is_eligible(task_id: str, board: str) -> bool:
    """Read committed state without creating/migrating a board database."""
    db_path = _board_db_path(board)
    uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, body FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise _WakeFailure("board_read_failed") from exc

    if row is None or str(row["status"]) != "done":
        return False
    return _is_github_backed_body(row["body"])


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass


def _run_edge(edge_path: Path, board: str) -> WakeResult:
    """Run one fixed edge command with timeout and combined-output bounds."""
    command = [sys.executable, str(edge_path), "--board", board, "--json"]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(edge_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        raise _WakeFailure("edge_spawn_failed") from exc

    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output_bytes = 0
    deadline = time.monotonic() + _EDGE_TIMEOUT_SECONDS
    stream_closed = False
    try:
        while not stream_closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                return WakeResult(
                    returncode=process.returncode,
                    output_bytes=output_bytes,
                    timed_out=True,
                )
            events = selector.select(remaining)
            if not events:
                _stop_process(process)
                return WakeResult(
                    returncode=process.returncode,
                    output_bytes=output_bytes,
                    timed_out=True,
                )
            for key, _ in events:
                data = os.read(key.fd, 4096)
                if not data:
                    stream_closed = True
                    selector.unregister(key.fileobj)
                    break
                output_bytes += len(data)
                if output_bytes > _EDGE_OUTPUT_LIMIT_BYTES:
                    _stop_process(process)
                    return WakeResult(
                        returncode=process.returncode,
                        output_bytes=output_bytes,
                        output_limited=True,
                    )
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        return WakeResult(returncode=returncode, output_bytes=output_bytes)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _stop_process(process)
        raise _WakeFailure("edge_process_failed") from exc
    finally:
        try:
            selector.close()
        finally:
            process.stdout.close()


def _diagnostic(task_id: str, board: str | None, code: str) -> None:
    """Emit only bounded identifiers and stable failure classes."""
    safe_task_id = task_id if _TASK_ID_RE.fullmatch(task_id) else "<invalid>"
    safe_board = board if isinstance(board, str) and _BOARD_RE.fullmatch(board) else "<invalid>"
    _LOG.warning(
        "GitHub completion edge wake skipped or failed: task=%s board=%s code=%s",
        safe_task_id,
        safe_board,
        code,
    )


def _on_task_completed(
    *,
    task_id: str | None = None,
    board: str | None = None,
    **_: Any,
) -> None:
    """Wake the edge after a committed GitHub-backed completion only."""
    safe_task_id = (
        task_id
        if isinstance(task_id, str) and _TASK_ID_RE.fullmatch(task_id)
        else "<invalid>"
    )
    try:
        if not isinstance(task_id, str) or not task_id:
            raise _WakeFailure("missing_task_id")
        safe_board = _valid_board(board)
        if not _completion_is_eligible(task_id, safe_board):
            return
        edge_path = _edge_script_path()
        result = _run_edge(edge_path, safe_board)
        if result.timed_out:
            _diagnostic(safe_task_id, safe_board, "edge_timeout")
        elif result.output_limited:
            _diagnostic(safe_task_id, safe_board, "edge_output_limit")
        elif result.returncode != 0:
            _diagnostic(safe_task_id, safe_board, "edge_nonzero")
    except _WakeFailure as exc:
        _diagnostic(safe_task_id, board if isinstance(board, str) else None, exc.code)
    except Exception as exc:  # noqa: BLE001  # observer must never affect completion
        _diagnostic(safe_task_id, board if isinstance(board, str) else None, type(exc).__name__)


def register(ctx: Any) -> None:
    """Register the post-commit completion observer."""
    ctx.register_hook("kanban_task_completed", _on_task_completed)


__all__ = ["WakeResult", "register"]
