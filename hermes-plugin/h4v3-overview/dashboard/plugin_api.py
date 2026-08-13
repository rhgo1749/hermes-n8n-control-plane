"""Read-only H4V3 multi-board projection for the Hermes dashboard.

Mounted at ``/api/plugins/h4v3-overview/`` by Hermes' existing dashboard
plugin loader. The implementation reads the installed canonical Kanban board
registry and opens each existing SQLite database through ``mode=ro``. It never
initializes a schema, writes a task/event, or creates an Overview database.
"""
from __future__ import annotations

import importlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    from fastapi import APIRouter as _FastAPIRouter  # type: ignore[assignment]
    from fastapi import HTTPException as _FastAPIHTTPException  # type: ignore[assignment]
except Exception:  # pragma: no cover - local pure-helper tests
    class _FastAPIRouter:  # type: ignore[no-redef]
        def get(self, *_args: Any, **_kwargs: Any):
            return lambda fn: fn

    class _FastAPIHTTPException(Exception):  # type: ignore[no-redef]
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

APIRouter: Any = _FastAPIRouter
HTTPException: Any = _FastAPIHTTPException

try:
    kanban_db = importlib.import_module("hermes_cli.kanban_db")
except ImportError:  # pragma: no cover - tests can inject a source tree
    kanban_db = None  # type: ignore[assignment]

router = APIRouter()

_STATUS_ORDER = (
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "review",
    "done",
)
_REWORK_EVENT_KINDS = ("github_pr_rework",)
_MEANINGFUL_EVENT_KINDS = (
    "github_pr_rework",
    "github_pr_rework_retry",
    "github_operator_attention",
)
_HUMAN_MARKERS = (
    "needs_input",
    "needs maintainer",
    "review-required",
    "host_validation_required",
    "human_validation_required",
    "human review",
)
_MAX_RECENT_EVENTS = 200
_MAX_REASON_CHARS = 180


def _json_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_text(value: Any, limit: int = _MAX_REASON_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _display_name(metadata: Mapping[str, Any], slug: str) -> str:
    return str(metadata.get("name") or slug.replace("-", " ").title()).strip() or slug


def _public_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Keep DB paths and unknown board.json fields out of the browser API."""
    return {
        key: metadata.get(key)
        for key in ("slug", "name", "description", "icon", "color", "project_id", "archived")
        if metadata.get(key) is not None
    }


def _repository_from_key(value: Any) -> Optional[str]:
    match = re.match(r"^github:([^:]+/[^:]+):issue:\d+$", str(value or ""), re.I)
    return match.group(1) if match else None


def _kanban_url(slug: str, task_id: Optional[str] = None) -> str:
    # The board query is understood by the existing Kanban dashboard. The task
    # query is retained as a stable provenance/deep-link hint without copying
    # the Kanban drawer or adding a second task store here.
    query = f"board={slug}"
    if task_id:
        query += f"&task={task_id}"
    return f"/kanban?{query}"


def _read_only_connection(path: Path) -> sqlite3.Connection:
    """Open an existing Kanban DB without the writable canonical connect path."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _empty_counts() -> dict[str, int]:
    return {status: 0 for status in _STATUS_ORDER}


def _empty_board(metadata: Mapping[str, Any], *, error: Optional[str] = None) -> dict[str, Any]:
    slug = str(metadata.get("slug") or "default")
    return {
        "slug": slug,
        "name": _display_name(metadata, slug),
        "metadata": _public_metadata(metadata),
        "repositories": [],
        "counts": _empty_counts(),
        "rework_count": 0,
        "recent_meaningful": None,
        "tasks": [],
        "read_error": error,
        "kanban_url": _kanban_url(slug),
    }


def _load_board_projection(metadata: Mapping[str, Any]) -> dict[str, Any]:
    board = _empty_board(metadata)
    db_path = Path(str(metadata.get("db_path") or ""))
    if not db_path.is_file():
        return board

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = _read_only_connection(db_path)
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
        required = {"id", "title", "status", "assignee", "block_kind", "block_recurrences"}
        if not required.issubset(columns):
            raise RuntimeError("Kanban tasks schema is incompatible")
        optional = {
            "consecutive_failures": "0",
            "last_failure_error": "NULL",
            "idempotency_key": "NULL",
            "body": "NULL",
        }
        select_optional = ", ".join(
            f"{name}" if name in columns else f"{default} AS {name}"
            for name, default in optional.items()
        )
        rows = conn.execute(
            "SELECT id, title, status, assignee, block_kind, block_recurrences, "
            f"{select_optional} "
            "FROM tasks WHERE status != 'archived' ORDER BY created_at ASC, id ASC"
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        event_rows: list[sqlite3.Row] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            event_rows = conn.execute(
                f"SELECT task_id, kind, payload, created_at FROM task_events "
                f"WHERE task_id IN ({placeholders}) ORDER BY created_at DESC, id DESC LIMIT ?",
                (*ids, _MAX_RECENT_EVENTS),
            ).fetchall()
        events_by_task: dict[str, list[sqlite3.Row]] = {}
        for event in event_rows:
            events_by_task.setdefault(str(event["task_id"]), []).append(event)

        repositories: set[str] = set()
        tasks: list[dict[str, Any]] = []
        recent_meaningful: Optional[dict[str, Any]] = None
        rework_count = 0
        for row in rows:
            status = str(row["status"] or "")
            if status in board["counts"]:
                board["counts"][status] += 1
            task_id = str(row["id"])
            repository = _repository_from_key(row["idempotency_key"])
            if repository:
                repositories.add(repository)
            task_events = events_by_task.get(task_id, [])
            rework_events = [event for event in task_events if event["kind"] in _REWORK_EVENT_KINDS]
            rework_count += len(rework_events)

            attention = False
            attention_reason = ""
            evidence_text = str(row["body"] or "")
            for event in task_events:
                payload = _json_payload(event["payload"])
                event_text = " ".join(
                    str(payload.get(key) or "")
                    for key in ("reason", "diagnostic", "retry_reason")
                )
                evidence_text += " " + event_text
                if any(marker in event_text.casefold() for marker in _HUMAN_MARKERS):
                    attention = True
                    attention_reason = _safe_text(event_text)
                    break
            block_kind = str(row["block_kind"] or "")
            if status == "blocked" and block_kind in {"needs_input", "capability"}:
                attention = True
                attention_reason = block_kind
            elif not attention:
                lowered = evidence_text.casefold()
                marker = next((item for item in _HUMAN_MARKERS if item in lowered), None)
                if marker:
                    attention = True
                    attention_reason = marker

            task = {
                "id": task_id,
                "title": _safe_text(row["title"], 120),
                "status": status,
                "assignee": row["assignee"],
                "block_kind": row["block_kind"],
                "block_recurrences": int(row["block_recurrences"] or 0),
                "consecutive_failures": int(row["consecutive_failures"] or 0),
                "last_failure_error": _safe_text(row["last_failure_error"]),
                "attention": attention,
                "attention_reason": attention_reason or None,
                "rework_count": len(rework_events),
                "repository": repository,
                "kanban_url": _kanban_url(str(board["slug"]), task_id),
            }
            tasks.append(task)

            meaningful_events = [event for event in task_events if event["kind"] in _MEANINGFUL_EVENT_KINDS]
            if meaningful_events and recent_meaningful is None:
                event = meaningful_events[0]
                recent_meaningful = {
                    "kind": str(event["kind"]),
                    "created_at": int(event["created_at"]),
                    "task_id": task_id,
                    "reason": _safe_text(_json_payload(event["payload"]).get("reason")),
                }

        board["repositories"] = sorted(repositories, key=str.casefold)
        board["rework_count"] = rework_count
        board["recent_meaningful"] = recent_meaningful
        board["tasks"] = tasks
        return board
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        board["read_error"] = f"{type(exc).__name__}: {_safe_text(exc)}"
        return board
    finally:
        if conn is not None:
            conn.close()


def _need_you_reason(task: Mapping[str, Any]) -> Optional[str]:
    if task.get("status") != "blocked":
        return None
    if task.get("block_kind") in {"needs_input", "capability"}:
        return str(task["block_kind"])
    if task.get("attention") and task.get("attention_reason"):
        return str(task["attention_reason"])
    return None


def build_overview() -> dict[str, Any]:
    """Build the complete projection from current Kanban boards."""
    if kanban_db is None:
        raise RuntimeError("Hermes kanban_db is unavailable")
    boards = [_load_board_projection(dict(item)) for item in kanban_db.list_boards(include_archived=False)]
    summary = {
        "need_you": 0,
        "blocked": 0,
        "review": 0,
        "running": 0,
        "ready": 0,
        "active_workers": 0,
    }
    need_you: list[dict[str, Any]] = []
    for board in boards:
        for task in board["tasks"]:
            status = str(task["status"])
            if status in {"blocked", "review", "running", "ready"}:
                summary[status] += 1
            if status == "running":
                summary["active_workers"] += 1
            reason = _need_you_reason(task)
            if reason is not None:
                summary["need_you"] += 1
                need_you.append({
                    "board": board["slug"],
                    "board_name": board["name"],
                    "task": task,
                    "reason": reason,
                })
    return {
        "schema_version": 1,
        "generated_at": int(time.time()),
        "read_only": True,
        "summary": summary,
        "need_you": need_you,
        "boards": boards,
        "deep_links": {"kanban_path": "/kanban"},
    }


@router.get("/overview")
def overview() -> dict[str, Any]:
    try:
        return build_overview()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Overview unavailable: {_safe_text(exc)}") from exc


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "read_only": True}
