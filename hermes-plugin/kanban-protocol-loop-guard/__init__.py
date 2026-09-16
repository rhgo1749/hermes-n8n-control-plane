"""H4V3 safety guard for repeated Kanban timeout/protocol failures.

Hermes remains the lifecycle/retry owner.  This observer only converts the
fifth consecutive failure in the bounded safety class into an explicit sticky
operator block.  The safety class is:

* max-runtime ``timed_out``; or
* clean-exit ``crashed`` with protocol-violation evidence.

``rate_limited`` runs are ignored rather than consuming or breaking the streak.
All other terminal outcomes break the streak.  The dispatch-tick hook is needed
because Hermes max-runtime enforcement does not emit ``on_kanban_worker_exited``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

_LOG = logging.getLogger(__name__)
_BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TASK_RE = re.compile(r"^t_[0-9a-f]{8,64}$")
_LIMIT = 5
_REASON = (
    "Safety stop: 5 consecutive bounded Kanban failures "
    "(max-runtime timeout or clean-exit lifecycle protocol violation). "
    "Operator review required."
)


def _metadata(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _is_protocol_violation(row: Any) -> bool:
    outcome = str(row["outcome"] or "")
    if outcome != "crashed":
        return False
    data = _metadata(row["metadata"])
    error = str(row["error"] or "").lower()
    return bool(data.get("protocol_violation")) or "protocol violation" in error


def _bounded_failure_streak(conn: Any, task_id: str) -> int:
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 64",
        (task_id,),
    ).fetchall()
    streak = 0
    for row in rows:
        outcome = str(row["outcome"] or "")
        if outcome == "rate_limited":
            continue
        if outcome == "timed_out" or _is_protocol_violation(row):
            streak += 1
            continue
        break
    return streak


def _connect_board(board: str) -> Any:
    from hermes_cli.kanban_db_connect import connect

    return connect(board=board)


def _promote(conn: Any, task_id: str, streak: int) -> bool:
    from hermes_cli.kanban_db import promote_task

    promoted, reason = promote_task(
        conn,
        task_id,
        actor="kanban-protocol-loop-guard",
        reason=f"prepare sticky safety block at bounded failure streak={streak}",
    )
    if not promoted:
        _LOG.warning(
            "kanban loop guard could not promote task before sticky block: task=%s reason=%s",
            task_id,
            reason,
        )
    return bool(promoted)


def _terminate_running(row: Any) -> bool:
    from hermes_cli.kanban_db import _terminate_reclaimed_worker

    info = _terminate_reclaimed_worker(
        row["worker_pid"],
        row["claim_lock"],
        started_at=row["worker_started_at"],
    )
    return bool(info.get("terminated"))


def _block(conn: Any, task_id: str, *, expected_run_id: int | None = None) -> bool:
    from hermes_cli.kanban_db import block_task

    return bool(
        block_task(
            conn,
            task_id,
            reason=_REASON,
            kind=None,
            expected_run_id=expected_run_id,
        )
    )


def _enforce_if_needed(conn: Any, task_id: str, board: str) -> bool:
    streak = _bounded_failure_streak(conn, task_id)
    if streak < _LIMIT:
        return False
    row = conn.execute(
        "SELECT status, worker_pid, worker_started_at, claim_lock, current_run_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False
    status = str(row["status"] or "")
    if status == "blocked":
        if not _promote(conn, task_id, streak):
            return False
        status = "ready"
    elif status == "running":
        # A dispatch tick normally leaves a fifth failure blocked.  This path is
        # only a defensive fallback for legacy/mismatched retry configuration.
        # Never clear the task ownership beside a live worker we could not prove
        # and terminate.
        if not _terminate_running(row):
            _LOG.error(
                "KANBAN_LOOP_GUARD refused sticky block beside live/unverified worker "
                "board=%s task=%s streak=%d",
                board,
                task_id,
                streak,
            )
            return False
        if _block(conn, task_id, expected_run_id=row["current_run_id"]):
            _LOG.error(
                "KANBAN_LOOP_GUARD blocked running fallback board=%s task=%s streak=%d",
                board,
                task_id,
                streak,
            )
            return True
        return False
    if status == "ready" and _block(conn, task_id):
        _LOG.error(
            "KANBAN_LOOP_GUARD blocked board=%s task=%s streak=%d",
            board,
            task_id,
            streak,
        )
        return True
    return False


def _valid_scope(board: Any, task_id: Any) -> bool:
    return (
        isinstance(board, str)
        and bool(_BOARD_RE.fullmatch(board))
        and isinstance(task_id, str)
        and bool(_TASK_RE.fullmatch(task_id))
    )


def _enforce_task(board: str, task_id: str) -> None:
    conn = _connect_board(board)
    try:
        _enforce_if_needed(conn, task_id, board)
    finally:
        conn.close()


def _on_worker_exited(
    *,
    task_id: str | None = None,
    board: str | None = None,
    exit_kind: str | None = None,
    outcome: str | None = None,
    retry_status: str | None = None,
    **_: Any,
) -> None:
    if exit_kind != "clean_exit" or outcome != "crashed" or retry_status != "ready":
        return
    if not _valid_scope(board, task_id):
        return
    try:
        _enforce_task(board, task_id)
    except Exception:
        _LOG.exception(
            "kanban loop guard worker-exit observer failed without changing core ownership: "
            "board=%s task=%s",
            board,
            task_id,
        )


def _on_dispatch_tick(
    *,
    board: str | None = None,
    dry_run: bool = False,
    result: Any = None,
    **_: Any,
) -> None:
    if dry_run or not isinstance(board, str) or not _BOARD_RE.fullmatch(board):
        return
    timed_out = getattr(result, "timed_out", None)
    if not isinstance(timed_out, (list, tuple, set, frozenset)):
        return
    for task_id in dict.fromkeys(timed_out):
        if not isinstance(task_id, str) or not _TASK_RE.fullmatch(task_id):
            continue
        try:
            _enforce_task(board, task_id)
        except Exception:
            _LOG.exception(
                "kanban loop guard dispatch-tick observer failed without changing core ownership: "
                "board=%s task=%s",
                board,
                task_id,
            )


def register(ctx: Any) -> None:
    ctx.register_hook("on_kanban_worker_exited", _on_worker_exited)
    ctx.register_hook("on_kanban_dispatch_tick", _on_dispatch_tick)


__all__ = [
    "register",
    "_bounded_failure_streak",
    "_enforce_if_needed",
    "_on_worker_exited",
    "_on_dispatch_tick",
]
