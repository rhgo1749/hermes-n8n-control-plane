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
from urllib.parse import quote
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
# Board-level rework aggregates must reflect current operational risk only:
# rework on finished cards must not look like an active problem.
_ACTIONABLE_STATUSES = frozenset({"ready", "running", "review", "blocked"})
_HUMAN_MARKERS = (
    "needs_input",
    "needs maintainer",
    "review-required",
    "host_validation_required",
    "human_validation_required",
    "human review",
)
_ATTENTION_EVENT_KINDS = frozenset({
    "github_operator_attention",
    "github_pr_rework_attention",
})
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


def _human_attention_reason(event: Any) -> Optional[str]:
    payload = _json_payload(event["payload"])
    event_text = " ".join(
        str(payload.get(key) or "")
        for key in ("reason", "diagnostic", "retry_reason")
    )
    if any(marker in event_text.casefold() for marker in _HUMAN_MARKERS):
        return _safe_text(event_text)
    return None


def _event_cursors(
    conn: sqlite3.Connection,
    task_ids: list[str],
    *,
    excluded_kinds: frozenset[str],
) -> dict[str, int]:
    if not task_ids:
        return {}
    task_placeholders = ",".join("?" for _ in task_ids)
    kind_placeholders = ",".join("?" for _ in excluded_kinds)
    rows = conn.execute(
        "SELECT task_id, MAX(id) AS cursor FROM task_events "
        f"WHERE task_id IN ({task_placeholders}) "
        f"AND kind NOT IN ({kind_placeholders}) GROUP BY task_id",
        (*task_ids, *sorted(excluded_kinds)),
    ).fetchall()
    return {str(row["task_id"]): int(row["cursor"]) for row in rows}


def _attention_key_cursor(payload: Mapping[str, Any]) -> Optional[int]:
    raw_key = str(payload.get("attention_key") or "")
    if ":" not in raw_key:
        return None
    suffix = raw_key.rsplit(":", 1)[1]
    return int(suffix) if suffix.isdigit() else None


def _load_attention_events(
    conn: sqlite3.Connection,
    task_ids: list[str],
) -> dict[str, sqlite3.Row]:
    """Load the newest unresolved explicit human-attention evidence.

    The recent-event window is intentionally bounded for board activity. It
    must not also bound durable attention evidence: an active task can remain
    actionable after more than ``_MAX_RECENT_EVENTS`` unrelated events. An
    attention event is unresolved only while its non-attention cursor remains
    current. ``github_operator_attention`` reuses the edge's
    ``attention_key=reason:<max-event-id-excluding-operator-attention>``
    contract; legacy attention events fall back to their event id against the
    lifecycle cursor.
    """
    if not task_ids:
        return {}
    task_placeholders = ",".join("?" for _ in task_ids)
    marker_clauses = " OR ".join(
        "instr(lower(COALESCE(payload, '')), ?) > 0"
        for _ in _HUMAN_MARKERS
    )
    candidates = conn.execute(
        "SELECT id, task_id, kind, payload, created_at FROM task_events "
        f"WHERE task_id IN ({task_placeholders}) AND ({marker_clauses}) "
        "ORDER BY created_at DESC, id DESC",
        (*task_ids, *(marker.casefold() for marker in _HUMAN_MARKERS)),
    ).fetchall()
    operator_cursors = _event_cursors(
        conn,
        task_ids,
        excluded_kinds=frozenset({"github_operator_attention"}),
    )
    lifecycle_cursors = _event_cursors(
        conn,
        task_ids,
        excluded_kinds=_ATTENTION_EVENT_KINDS,
    )
    evidence: dict[str, sqlite3.Row] = {}
    for event in candidates:
        task_id = str(event["task_id"])
        if task_id in evidence:
            continue
        if _human_attention_reason(event) is None:
            continue
        payload = _json_payload(event["payload"])
        if event["kind"] == "github_operator_attention":
            cursor = _attention_key_cursor(payload)
            if cursor is None:
                unresolved = lifecycle_cursors.get(task_id, 0) <= int(event["id"])
            else:
                unresolved = operator_cursors.get(task_id, 0) <= cursor
        else:
            unresolved = lifecycle_cursors.get(task_id, 0) <= int(event["id"])
        if unresolved:
            evidence[task_id] = event
    return evidence


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


def _latest_rework_payload(events: list[sqlite3.Row]) -> dict[str, Any]:
    """Newest actionable rework event payload (events are newest-first)."""
    for event in events:
        payload = _json_payload(event["payload"])
        if payload.get("repository") and payload.get("pr_number"):
            return payload
    return {}


def _github_pr_url(repository: Any, pr_number: Any) -> Optional[str]:
    """Canonical GitHub PR URL from rework event provenance (read-only link)."""
    repo = str(repository or "").strip()
    number = str(pr_number or "").strip()
    if repo and number.isdigit() and "/" in repo and "://" not in repo:
        return f"https://github.com/{repo}/pull/{number}"
    return None


def _github_open_prs_url(repositories: list[str]) -> Optional[str]:
    """All open PRs across this board's repositories (no label filter)."""
    repos = [repo for repo in repositories if repo and "://" not in repo]
    if not repos:
        return None
    query = "is:pr is:open " + " ".join(
        f"repo:{repo}" for repo in sorted(repos)
    )
    return "https://github.com/pulls?q=" + quote(query.strip(), safe="")


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
        "open_prs_url": None,
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
        pending_parents_by_task: dict[str, list[dict[str, str]]] = {}
        block_projection_errors: dict[str, str] = {}
        if ids:
            placeholders = ",".join("?" for _ in ids)
            try:
                link_rows = conn.execute(
                    "SELECT l.child_id, l.parent_id, t.status AS parent_status "
                    "FROM task_links AS l LEFT JOIN tasks AS t ON t.id = l.parent_id "
                    f"WHERE l.child_id IN ({placeholders}) "
                    "ORDER BY l.child_id, l.parent_id",
                    tuple(ids),
                ).fetchall()
                for link in link_rows:
                    parent = {
                        "id": str(link["parent_id"]),
                        "status": str(link["parent_status"] or "missing"),
                    }
                    if parent["status"] not in {"done", "archived"}:
                        pending_parents_by_task.setdefault(
                            str(link["child_id"]), []
                        ).append(parent)
            except sqlite3.Error as exc:
                error = f"{type(exc).__name__}: {_safe_text(exc)}"
                block_projection_errors = {
                    str(row["id"]): error
                    for row in rows
                    if str(row["status"] or "") == "blocked"
                }
        event_rows: list[sqlite3.Row] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            event_rows = conn.execute(
                f"SELECT id, task_id, kind, payload, created_at FROM task_events "
                f"WHERE task_id IN ({placeholders}) ORDER BY created_at DESC, id DESC LIMIT ?",
                (*ids, _MAX_RECENT_EVENTS),
            ).fetchall()
        events_by_task: dict[str, list[sqlite3.Row]] = {}
        for event in event_rows:
            events_by_task.setdefault(str(event["task_id"]), []).append(event)
        attention_task_ids = [
            str(row["id"])
            for row in rows
            if str(row["status"] or "") not in {"done", "archived"}
        ]
        attention_events_by_task = _load_attention_events(conn, attention_task_ids)

        # The truly newest meaningful event on the board: event_rows are
        # globally ordered newest-first, so the first matching row wins
        # regardless of which task it belongs to.
        recent_meaningful: Optional[dict[str, Any]] = None
        for event in event_rows:
            if event["kind"] not in _MEANINGFUL_EVENT_KINDS:
                continue
            recent_meaningful = {
                "kind": str(event["kind"]),
                "created_at": int(event["created_at"]),
                "event_id": int(event["id"]),
                "task_id": str(event["task_id"]),
                "reason": _safe_text(_json_payload(event["payload"]).get("reason")),
            }
            break

        repositories: set[str] = set()
        tasks: list[dict[str, Any]] = []
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
            if status in _ACTIONABLE_STATUSES:
                rework_count += len(rework_events)
            latest_rework = _latest_rework_payload(rework_events)

            attention = False
            attention_reason = ""
            # Evidence is taken from the durable event stream only. The intake
            # card body is excluded: it contains static contract prose
            # ("keep HUMAN_VALIDATION_REQUIRED / HOST_VALIDATION_REQUIRED /
            # BLOCKED states honest") that would false-positive every card.
            attention_event = attention_events_by_task.get(task_id)
            if attention_event is not None:
                attention_reason = _human_attention_reason(attention_event) or ""
                attention = bool(attention_reason)
            block_kind = str(row["block_kind"] or "")
            if status == "blocked" and block_kind in {"needs_input", "capability"}:
                attention = True
                attention_reason = block_kind

            block: dict[str, Any] | None = None
            if status == "blocked":
                projected_kind = block_kind or "untyped"
                pending = pending_parents_by_task.get(task_id, [])
                block = {
                    "block_kind": projected_kind,
                    "pending_parent_ids": [item["id"] for item in pending],
                    "pending_parents": pending,
                    "dependency_driven": projected_kind == "dependency",
                    "auto_promotable": projected_kind == "dependency",
                }
                if task_id in block_projection_errors:
                    block["projection_error"] = block_projection_errors[task_id]
                    block["auto_promotable"] = False

            task = {
                "id": task_id,
                "title": _safe_text(row["title"], 120),
                "status": status,
                "assignee": row["assignee"],
                "block_kind": block["block_kind"] if block else row["block_kind"],
                "block": block,
                "auto_promotable": block["auto_promotable"] if block else False,
                "block_recurrences": int(row["block_recurrences"] or 0),
                "consecutive_failures": int(row["consecutive_failures"] or 0),
                "last_failure_error": _safe_text(row["last_failure_error"]),
                "attention": attention,
                "attention_reason": attention_reason or None,
                "rework_count": len(rework_events),
                "repository": repository,
                "kanban_url": _kanban_url(str(board["slug"]), task_id),
                "rework_pr_url": _github_pr_url(
                    latest_rework.get("repository"), latest_rework.get("pr_number")
                ),
                "rework_pr_number": (
                    int(latest_rework["pr_number"])
                    if str(latest_rework.get("pr_number") or "").isdigit() else None
                ),
            }
            tasks.append(task)

        board["repositories"] = sorted(repositories, key=str.casefold)
        board["open_prs_url"] = _github_open_prs_url(board["repositories"])
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
    """Project current operator attention from durable task evidence.

    Rules:
    * terminal ``done``/``archived`` tasks are never Need You, even when
      historical attention evidence remains in the event stream
    * ``blocked`` + ``block_kind`` in {needs_input, capability} → Need You
    * any non-terminal status with explicit human-validation / maintainer-attention
      evidence (attention event markers) → Need You
    * plain REVIEW, or plain BLOCKED without evidence → not Need You
    """
    status = task.get("status")
    if status in {"done", "archived"}:
        return None
    if status == "blocked" and task.get("block_kind") in {"needs_input", "capability"}:
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
