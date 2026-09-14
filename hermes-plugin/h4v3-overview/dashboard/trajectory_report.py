"""Read-only trajectory/evaluation reports for the H4V3 Overview plugin."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCHEMA_VERSION = 1
SCHEMA_ID = "h4v3-trajectory-v1"
_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens",
)
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)
_VERDICT_RE = re.compile(r"^(PASS|REWORK)$", re.IGNORECASE)
_TOOL_FAILURE_KINDS = frozenset({"tool_failed", "tool_failure", "tool_error"})
_TOOL_RETRY_KINDS = frozenset({"tool_retry", "tool_retried"})


class TrajectoryInputError(ValueError):
    """Raised for an invalid selector, not for missing telemetry."""


def _now(value: Optional[int]) -> int:
    return int(time.time()) if value is None else int(value)


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _safe_id(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or len(text) > 256 or any(ord(ch) < 32 for ch in text):
        return None
    return text


def _validate_repository(repository: str) -> str:
    value = str(repository or "").strip()
    if value.endswith(".git"):
        value = value[:-4]
    if not _REPOSITORY_RE.fullmatch(value) or "://" in value:
        raise TrajectoryInputError("repository must be an owner/repository slug")
    return value


def _read_only_connection(path: Path) -> sqlite3.Connection:
    """Open an existing database without invoking any schema writer."""
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _quoted_columns(columns: Iterable[str], available: set[str]) -> str:
    return ", ".join(
        f'"{column}"' if column in available else f'NULL AS "{column}"'
        for column in columns
    )


def _select_task_rows(
    conn: sqlite3.Connection, ids: Optional[Sequence[str]] = None,
) -> dict[str, dict[str, Any]]:
    available = _table_columns(conn, "tasks")
    if "id" not in available:
        return {}
    columns = (
        "id", "status", "assignee", "created_by", "created_at",
        "started_at", "completed_at", "project_id", "idempotency_key",
    )
    sql = f"SELECT {_quoted_columns(columns, available)} FROM tasks"
    params: list[Any] = []
    if ids is not None:
        clean = [item for item in (_safe_id(value) for value in ids) if item]
        if not clean:
            return {}
        sql += " WHERE id IN (" + ",".join("?" for _ in clean) + ")"
        params.extend(clean)
    try:
        rows = conn.execute(sql + " ORDER BY COALESCE(created_at, 0), id", params).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row["id"]): dict(row) for row in rows}


def _select_links(conn: sqlite3.Connection) -> list[dict[str, str]]:
    if not {"parent_id", "child_id"}.issubset(_table_columns(conn, "task_links")):
        return []
    try:
        rows = conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY parent_id, child_id"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"parent_id": str(row["parent_id"]), "child_id": str(row["child_id"])}
        for row in rows
        if row["parent_id"] is not None and row["child_id"] is not None
    ]


def _select_runs(conn: sqlite3.Connection, task_ids: Sequence[str]) -> list[dict[str, Any]]:
    available = _table_columns(conn, "task_runs")
    if not {"id", "task_id"}.issubset(available) or not task_ids:
        return []
    columns = (
        "id", "task_id", "profile", "status", "outcome", "started_at",
        "ended_at", "metadata", "summary",
    )
    placeholders = ",".join("?" for _ in task_ids)
    sql = (
        f"SELECT {_quoted_columns(columns, available)} FROM task_runs "
        f"WHERE task_id IN ({placeholders}) ORDER BY COALESCE(started_at, 0), id"
    )
    try:
        rows = conn.execute(sql, list(task_ids)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _select_events(conn: sqlite3.Connection, task_ids: Sequence[str]) -> list[dict[str, Any]]:
    available = _table_columns(conn, "task_events")
    if not {"id", "task_id", "kind"}.issubset(available) or not task_ids:
        return []
    columns = ("id", "task_id", "run_id", "kind", "payload", "created_at")
    placeholders = ",".join("?" for _ in task_ids)
    sql = (
        f"SELECT {_quoted_columns(columns, available)} FROM task_events "
        f"WHERE task_id IN ({placeholders}) ORDER BY COALESCE(created_at, 0), id"
    )
    try:
        rows = conn.execute(sql, list(task_ids)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _run_metadata(run: Mapping[str, Any]) -> dict[str, Any]:
    return _json_dict(run.get("metadata"))


def _role(task: Mapping[str, Any], runs: Sequence[Mapping[str, Any]]) -> str:
    for raw in [task.get("assignee"), *(run.get("profile") for run in runs)]:
        value = str(raw or "").casefold()
        for role in ("reviewer", "investigator", "developer", "designer"):
            if role in value:
                return role
    return "other"


def _exact_verdict(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if _VERDICT_RE.fullmatch(value) else None


def _trusted_verdict(run: Mapping[str, Any]) -> Optional[str]:
    """Use explicit reviewer metadata or a complete terminal line only."""
    metadata = _run_metadata(run)
    for key in ("verdict", "review_verdict", "review_outcome", "terminal_verdict"):
        verdict = _exact_verdict(metadata.get(key))
        if verdict:
            return verdict
    for value in (metadata.get("result"), run.get("summary")):
        if not isinstance(value, str):
            continue
        for line in value.splitlines():
            token = re.sub(r"[.!:]+$", "", line.strip()).upper()
            if _VERDICT_RE.fullmatch(token):
                return token
    return None


def _has_model_refresh_marker(
    run: Mapping[str, Any], payloads: Sequence[Mapping[str, Any]],
) -> bool:
    metadata = _run_metadata(run)
    values = [metadata.get("model_refresh"), metadata.get("investigation_model_refresh")]
    if any(value is True or (isinstance(value, str) and value.strip().casefold() == "true") for value in values):
        return True
    if str(metadata.get("category") or "").casefold() == "investigation_model_refresh":
        return True
    for payload in payloads:
        if payload.get("model_refresh") is True:
            return True
        if str(payload.get("category") or "").casefold() == "investigation_model_refresh":
            return True
    return False


def _event_is_operator_block(event: Mapping[str, Any]) -> bool:
    kind = str(event.get("kind") or "")
    payload = _json_dict(event.get("payload"))
    block_kind = str(payload.get("kind") or payload.get("block_kind") or "").casefold()
    if kind in {"blocked", "block_loop_detected"} and block_kind in {"needs_input", "capability"}:
        return True
    if kind == "github_operator_attention":
        text = " ".join(str(payload.get(key) or "").casefold() for key in ("reason", "diagnostic"))
        return any(marker in text for marker in ("needs_input", "needs maintainer", "human_validation", "review-required"))
    return False


def _event_is_dependency_block(event: Mapping[str, Any]) -> bool:
    payload = _json_dict(event.get("payload"))
    return str(payload.get("kind") or payload.get("block_kind") or "").casefold() == "dependency"


def _paired_wait_seconds(
    events: Sequence[Mapping[str, Any]], *,
    start_predicate: Callable[[Mapping[str, Any]], bool],
    end_predicate: Callable[[Mapping[str, Any]], bool],
) -> Optional[int]:
    total = 0
    starts: list[int] = []
    paired = 0
    ordered = sorted(events, key=lambda item: (_as_int(item.get("created_at")) or 0, _as_int(item.get("id")) or 0))
    for event in ordered:
        at = _as_int(event.get("created_at"))
        if at is None:
            continue
        if start_predicate(event):
            starts.append(at)
        elif starts and end_predicate(event):
            start = starts.pop(0)
            if at >= start:
                total += at - start
                paired += 1
    return total if paired else None


def _select_scope(
    all_tasks: Mapping[str, Mapping[str, Any]],
    links: Sequence[Mapping[str, str]],
    *,
    repository: str,
    issue: int,
    root_task_id: Optional[str],
    task_ids: Optional[Sequence[str]],
) -> tuple[Optional[str], str, Optional[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Select by exact provenance and graph edges; never title/body text."""
    exact_key = f"github:{repository}:issue:{issue}"
    exact_roots = [task for task in all_tasks.values() if task.get("idempotency_key") == exact_key]
    root: Optional[dict[str, Any]] = None
    root_status = "known"
    method = "idempotency_key"
    if root_task_id:
        method = "explicit_root_task_id"
        root_task_id = _safe_id(root_task_id)
        root_row = all_tasks.get(root_task_id or "")
        root = dict(root_row) if root_row is not None else None
        if root is None:
            root_status = "unavailable"
        elif root.get("idempotency_key") != exact_key:
            # An explicit ID is still subordinate to the repository/issue
            # anchor. Never let a caller turn an unrelated card into a root.
            root = None
            root_task_id = None
            root_status = "unknown"
            method = "explicit_root_task_id_mismatch"
    elif len(exact_roots) == 1:
        root = dict(exact_roots[0])
        root_task_id = str(root["id"])
    elif len(exact_roots) > 1:
        root_status = "unknown"
        method = "ambiguous_idempotency_key"
        root_task_id = None
    else:
        root_status = "unavailable"
        method = "strict_idempotency_scope"
        root_task_id = None

    selected: set[str] = set()
    if task_ids is not None:
        method = "explicit_task_manifest"
        selected.update(item for item in (_safe_id(value) for value in task_ids) if item)
    else:
        if root is not None:
            selected.add(str(root["id"]))
        # The root row can be absent after archival/retention. A strict source
        # key still gives a bounded seed; graph closure recovers linked rounds.
        selected.update(
            task_id for task_id, task in all_tasks.items()
            if str(task.get("idempotency_key") or "").startswith(exact_key + ":")
        )
        canary_prefix = f"issue{issue}-canary-"
        selected.update(
            task_id for task_id, task in all_tasks.items()
            if str(task.get("idempotency_key") or "").startswith(canary_prefix)
        )
    adjacency: dict[str, set[str]] = defaultdict(set)
    for link in links:
        parent, child = link["parent_id"], link["child_id"]
        adjacency[parent].add(child)
        adjacency[child].add(parent)
    queue = list(selected)
    seen = set(selected)
    while queue:
        current = queue.pop(0)
        for neighbor in adjacency.get(current, set()):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    selected_tasks = {
        task_id: dict(all_tasks[task_id])
        for task_id in seen if task_id in all_tasks
    }
    provenance = {
        "method": method,
        "exact_source_key": exact_key,
        "requested_root_task_id": root_task_id,
        "root_task_status": root_status,
        "selected_task_ids_explicit": task_ids is not None,
        "selected_task_count": len(selected_tasks),
        "body_or_title_matching": False,
    }
    return root_task_id, root_status, root, selected_tasks, provenance


def _load_session_usage(run: Mapping[str, Any], profile_root: Path) -> dict[str, Any]:
    metadata = _run_metadata(run)
    session_id = _safe_id(metadata.get("worker_session_id"))
    result: dict[str, Any] = {
        "run_id": _as_int(run.get("id")),
        "profile": str(run.get("profile") or "unknown"),
        "worker_session_id": session_id,
        "availability": "unavailable",
        "session": None,
        "model_rows": [],
        "reason": None,
    }
    if session_id is None:
        result["reason"] = "worker_session_id_missing"
        return result
    profile = str(run.get("profile") or "")
    if not _PROFILE_RE.fullmatch(profile):
        result["reason"] = "profile_name_invalid"
        return result
    try:
        conn = _read_only_connection(profile_root / profile / "state.db")
    except (OSError, sqlite3.Error):
        result["reason"] = "state_db_unavailable"
        return result
    try:
        session_columns = _table_columns(conn, "sessions")
        if "id" not in session_columns:
            result["reason"] = "sessions_table_unavailable"
            return result
        columns = (
            "id", "started_at", "ended_at", "model", "billing_provider",
            *_TOKEN_FIELDS, "tool_call_count",
        )
        row = conn.execute(
            f"SELECT {_quoted_columns(columns, session_columns)} FROM sessions WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            result["reason"] = "session_row_unavailable"
            return result
        result["session"] = dict(row)
        usage_columns = _table_columns(conn, "session_model_usage")
        if {"session_id", "model"}.issubset(usage_columns):
            model_columns = (
                "model", "billing_provider", *_TOKEN_FIELDS,
            )
            result["model_rows"] = [
                dict(item) for item in conn.execute(
                    f"SELECT {_quoted_columns(model_columns, usage_columns)} FROM session_model_usage "
                    "WHERE session_id = ? ORDER BY model, billing_provider",
                    (session_id,),
                ).fetchall()
            ]
        result["availability"] = "known"
        if any(result["session"].get(field) is None for field in (*_TOKEN_FIELDS, "tool_call_count")):
            result["availability"] = "partial"
        return result
    except (sqlite3.Error, TypeError, ValueError):
        result["reason"] = "state_db_schema_error"
        return result
    finally:
        conn.close()


def _usage_report(
    runs: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]], *,
    profile_root: Optional[Path],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = profile_root or Path(os.environ.get("HERMES_HOME", "/home/hermes/.hermes")) / "profiles"
    records = [_load_session_usage(run, root) for run in runs]
    known = [record for record in records if record.get("session") is not None]
    totals: dict[str, Optional[int]] = {}
    field_availability: dict[str, str] = {}
    for field in (*_TOKEN_FIELDS, "tool_call_count"):
        values = [_as_int(record["session"].get(field)) for record in known]
        values = [value for value in values if value is not None]
        totals[field] = sum(values) if values else None
        field_availability[field] = (
            "known" if len(values) == len(runs) and runs else
            "partial" if values else "unavailable"
        )
    if totals["input_tokens"] is not None and totals["output_tokens"] is not None:
        totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
        field_availability["total_tokens"] = "known" if len(known) == len(runs) and runs else "partial"
    else:
        totals["total_tokens"] = None
        field_availability["total_tokens"] = "unavailable"
    totals["total_tokens_input_plus_output"] = totals["total_tokens"]
    field_availability["total_tokens_input_plus_output"] = field_availability["total_tokens"]

    def new_bucket(
        fields: Sequence[str], count_key: str, known_key: str,
    ) -> dict[str, Any]:
        return {
            count_key: 0,
            known_key: 0,
            "availability": "unavailable",
            "field_availability": {field: "unavailable" for field in fields},
            "coverage": {
                field: {"known": 0, "total": 0, "fraction": None}
                for field in fields
            },
            "known_only_totals": {field: None for field in fields},
            **{field: None for field in fields},
            "total_tokens": None,
            "total_tokens_input_plus_output": None,
            "_fields": tuple(fields),
            "_field_known": {field: 0 for field in fields},
            "_field_totals": {field: None for field in fields},
            "_complete_rows": 0,
            "_complete_total": None,
            "_count_key": count_key,
        }

    def add_bucket_row(
        bucket: dict[str, Any], row: Optional[Mapping[str, Any]],
        *, known_key: str,
    ) -> None:
        bucket[bucket["_count_key"]] += 1
        if row is None:
            return
        bucket[known_key] += 1
        values = {
            field: _as_int(row.get(field))
            for field in bucket["_fields"]
        }
        for field, value in values.items():
            if value is None:
                continue
            bucket["_field_known"][field] += 1
            previous = bucket["_field_totals"][field]
            bucket["_field_totals"][field] = (
                value if previous is None else previous + value
            )
        input_value = values.get("input_tokens")
        output_value = values.get("output_tokens")
        if input_value is not None and output_value is not None:
            bucket["_complete_rows"] += 1
            complete_total = input_value + output_value
            previous = bucket["_complete_total"]
            bucket["_complete_total"] = (
                complete_total
                if previous is None else previous + complete_total
            )

    def finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
        total_rows = int(bucket[bucket["_count_key"]])
        statuses: list[str] = []
        for field in bucket["_fields"]:
            known = int(bucket["_field_known"][field])
            total = bucket["_field_totals"][field]
            availability = (
                "known" if total_rows > 0 and known == total_rows else
                "partial" if known > 0 else "unavailable"
            )
            bucket["coverage"][field] = {
                "known": known,
                "total": total_rows,
                "fraction": known / total_rows if total_rows else None,
            }
            bucket["field_availability"][field] = availability
            bucket["known_only_totals"][field] = total
            bucket[field] = total
            statuses.append(availability)

        complete_rows = int(bucket["_complete_rows"])
        complete_total = bucket["_complete_total"]
        total_availability = (
            "known" if total_rows > 0 and complete_rows == total_rows else
            "partial" if complete_rows > 0 else "unavailable"
        )
        bucket["field_availability"]["total_tokens"] = total_availability
        bucket["field_availability"]["total_tokens_input_plus_output"] = total_availability
        bucket["known_only_totals"]["total_tokens"] = complete_total
        bucket["known_only_totals"]["total_tokens_input_plus_output"] = complete_total
        # A partial input/output pair is not a measured total or a zero.
        bucket["total_tokens"] = (
            complete_total if total_availability == "known" else None
        )
        bucket["total_tokens_input_plus_output"] = bucket["total_tokens"]
        statuses.append(total_availability)
        bucket["availability"] = (
            "known" if statuses and all(status == "known" for status in statuses) else
            "partial" if any(status in {"known", "partial"} for status in statuses)
            else "unavailable"
        )
        for private_key in (
            "_fields", "_field_known", "_field_totals", "_complete_rows",
            "_complete_total", "_count_key",
        ):
            bucket.pop(private_key, None)
        return bucket

    by_profile: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    profile_fields = (*_TOKEN_FIELDS, "tool_call_count")
    for record in records:
        session = record.get("session")
        profile = str(record.get("profile") or "unknown")
        profile_bucket = by_profile.setdefault(
            profile, new_bucket(profile_fields, "total_runs", "known_runs")
        )
        add_bucket_row(
            profile_bucket,
            session if isinstance(session, Mapping) else None,
            known_key="known_runs",
        )
        if not isinstance(session, Mapping):
            continue
        model_rows = list(record.get("model_rows") or [])
        if not model_rows and session.get("model") is not None:
            # The session row remains canonical model/provider evidence.
            model_rows = [{
                "model": session.get("model"),
                "billing_provider": session.get("billing_provider"),
                **{field: session.get(field) for field in _TOKEN_FIELDS},
            }]
        for row in model_rows:
            if not isinstance(row, Mapping):
                continue
            model = str(row.get("model") or "unknown")
            provider = str(row.get("billing_provider") or "unknown")
            key = f"{provider}:{model}"
            bucket = by_model.setdefault(
                key,
                {
                    "provider": provider,
                    "model": model,
                    **new_bucket(_TOKEN_FIELDS, "total_rows", "known_rows"),
                },
            )
            add_bucket_row(bucket, row, known_key="known_rows")

    by_profile = {
        key: finalize_bucket(value) for key, value in by_profile.items()
    }
    by_model = {
        key: finalize_bucket(value) for key, value in by_model.items()
    }

    statuses = list(field_availability.values())
    overall = "known" if statuses and all(status == "known" for status in statuses) else (
        "partial" if any(status in {"known", "partial"} for status in statuses) else "unavailable"
    )
    completed_run_ids = {
        _as_int(run.get("id")) for run in runs
        if str(run.get("outcome") or "").casefold() == "completed"
    }
    known_run_ids = {
        _as_int(record.get("run_id")) for record in known
    }
    completed_known = len(completed_run_ids & known_run_ids)
    failure_events = [event for event in events if str(event.get("kind") or "").casefold() in _TOOL_FAILURE_KINDS]
    retry_events = [event for event in events if str(event.get("kind") or "").casefold() in _TOOL_RETRY_KINDS]
    report = {
        "availability": overall,
        "coverage": {
            "all_runs": {"known": len(known), "total": len(runs), "fraction": len(known) / len(runs) if runs else None},
            "completed_runs": {"known": completed_known, "total": len(completed_run_ids), "fraction": completed_known / len(completed_run_ids) if completed_run_ids else None},
            "model_split_runs": {
                "known": sum(
                    bool(record.get("model_rows"))
                    or bool(isinstance(record.get("session"), Mapping) and record["session"].get("model"))
                    for record in records
                ),
                "total": len(runs),
            },
        },
        "totals": totals,
        "known_only_totals": dict(totals),
        "field_availability": field_availability,
        "by_profile": dict(sorted(by_profile.items())),
        "by_effective_model": dict(sorted(by_model.items())),
        "cost": {"value": None, "availability": "unavailable", "reason": "no_authoritative_per_task_charge"},
        "monetary_cost": {"value": None, "availability": "unavailable", "reason": "subscription_included_or_no_authoritative_per_task_charge"},
        "tool_failures": {
            "value": len(failure_events) if failure_events else None,
            "availability": "known" if failure_events else "unavailable",
            "reason": None if failure_events else "no_canonical_tool_failure_events",
        },
        "tool_retries": {
            "value": len(retry_events) if retry_events else None,
            "availability": "known" if retry_events else "unavailable",
            "reason": None if retry_events else "no_canonical_tool_retry_events",
        },
    }
    return report, records


def _github_auth_token() -> Optional[str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ("gh", "auth", "token"),
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    token = result.stdout.strip()
    return token or None


def _default_github_fetch(path: str) -> Any:
    token = _github_auth_token()
    url = path if path.startswith("http://") or path.startswith("https://") else "https://api.github.com" + path
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "h4v3-overview-trajectory/1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=8) as response:  # nosec B310 - fixed GitHub base or test URL
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, (Mapping, list)):
        raise ValueError("GitHub response was not an object or list")
    return payload


_GITHUB_CLOSING_REFERENCE_QUERY = chr(10).join((
    "query($owner:String!, $name:String!, $number:Int!) {",
    "  repository(owner:$owner, name:$name) {",
    "    pullRequest(number:$number) {",
    "      number",
    "      state",
    "      mergedAt",
    "      headRefOid",
    "      mergeCommit { oid }",
    "      repository { nameWithOwner }",
    "      closingIssuesReferences(first:100) {",
    "        nodes { number repository { nameWithOwner } }",
    "      }",
    "    }",
    "  }",
    "}",
))


def _default_github_graphql_fetch(
    query: str, variables: Mapping[str, Any],
) -> Mapping[str, Any]:
    token = _github_auth_token()
    payload = json.dumps({"query": query, "variables": dict(variables)}).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "h4v3-overview-trajectory/1",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(
        "https://api.github.com/graphql", data=payload, headers=headers, method="POST"
    )
    with urlopen(request, timeout=8) as response:  # nosec B310 - fixed GitHub endpoint
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, Mapping) or body.get("errors"):
        raise ValueError("GitHub GraphQL response contained errors")
    data = body.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("GitHub GraphQL response had no data object")
    return data


def _timeline_items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        for key in ("events", "timeline", "nodes"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, Mapping)]
    return []


def _timeline_pr_numbers(value: Any) -> list[int]:
    numbers: set[int] = set()
    for event in _timeline_items(value):
        event_name = str(event.get("event") or event.get("type") or "").casefold()
        if event_name not in {
            "cross-referenced", "cross_referenced", "crossreferencedevent",
        }:
            continue
        source = event.get("source")
        if not isinstance(source, Mapping):
            continue
        source_issue = source.get("issue")
        candidate = source_issue if isinstance(source_issue, Mapping) else source
        pull_request = candidate.get("pull_request") or candidate.get("pullRequest")
        urls = " ".join(str(candidate.get(key) or "") for key in ("html_url", "url"))
        is_pull_request = isinstance(pull_request, Mapping) or "/pull/" in urls
        if not is_pull_request:
            continue
        number = _as_int(candidate.get("number"))
        if number is not None and number > 0:
            numbers.add(number)
    return sorted(numbers)


def _closing_reference_nodes(value: Any) -> tuple[list[Mapping[str, Any]], bool]:
    if not isinstance(value, Mapping):
        return [], False
    for key in ("closingIssuesReferences", "closing_issues_references"):
        if key not in value:
            continue
        refs = value.get(key)
        if isinstance(refs, Mapping):
            refs = refs.get("nodes")
        if not isinstance(refs, list):
            return [], True
        return [item for item in refs if isinstance(item, Mapping)], True
    raw_numbers = value.get("closing_issue_numbers")
    if isinstance(raw_numbers, list):
        return [{
            "number": number
        } for number in raw_numbers if _as_int(number) is not None], True
    return [], False


def _has_closing_reference(
    value: Any, *, repository: str, issue: int,
) -> tuple[bool, bool]:
    nodes, available = _closing_reference_nodes(value)
    if not available:
        return False, False
    for node in nodes:
        if _as_int(node.get("number")) != issue:
            continue
        ref_repo = node.get("repository")
        if isinstance(ref_repo, Mapping):
            ref_repo = ref_repo.get("nameWithOwner") or ref_repo.get("full_name")
        if ref_repo is None or str(ref_repo) == repository:
            return True, True
    return False, True


def _graphql_pull_request(
    fetcher: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    repository: str, number: int,
) -> Mapping[str, Any]:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name:
        raise ValueError("repository must be an owner/repository slug")
    response = fetcher(
        _GITHUB_CLOSING_REFERENCE_QUERY,
        {"owner": owner, "name": name, "number": number},
    )
    data = response.get("data") if isinstance(response, Mapping) else None
    if isinstance(data, Mapping):
        response = data
    repo = response.get("repository") if isinstance(response, Mapping) else None
    pr = repo.get("pullRequest") if isinstance(repo, Mapping) else None
    if not isinstance(pr, Mapping):
        raise ValueError("GitHub GraphQL pull request was unavailable")
    return pr


def _github_unavailable_result(
    repository: str, issue: int, *, fetched_at: Optional[int],
    issue_data: Optional[Mapping[str, Any]], error: str,
) -> dict[str, Any]:
    issue_state = (
        str(issue_data.get("state") or "").lower() or None
        if isinstance(issue_data, Mapping) else None
    )
    return {
        "availability": "partial" if isinstance(issue_data, Mapping) else "unavailable",
        "fetched_at": fetched_at,
        "repository": repository,
        "repository_anchor_verified": False,
        "issue_number": issue,
        "issue_state": issue_state,
        "issue_closed": issue_state == "closed" if issue_state else None,
        "issue_closed_at": issue_data.get("closed_at") if isinstance(issue_data, Mapping) else None,
        "pr_number": None,
        "pr_state": None,
        "pr_merged_at": None,
        "observed_pr_head_sha": None,
        "merge_commit_sha": None,
        "outcome": None,
        "issue_closure_authoritative": False,
        "closing_reference_verified": False,
        "pr_discovery": None,
        "error": error,
    }


def _github_outcome(
    repository: str,
    issue: int,
    events: Sequence[Mapping[str, Any]],
    *,
    github_evidence: Optional[Mapping[str, Any]],
    github_fetcher: Optional[Callable[[str], Any]],
    github_graphql_fetcher: Optional[Callable[[str, Mapping[str, Any]], Mapping[str, Any]]],
    generated_at: int,
) -> dict[str, Any]:
    # Read Issue and PR evidence separately; Kanban state is never outcome.
    fetch = github_fetcher or _default_github_fetch
    supplied = dict(github_evidence or {})
    issue_data = supplied.get("issue") if isinstance(supplied.get("issue"), Mapping) else None
    pr_data = supplied.get("pr") if isinstance(supplied.get("pr"), Mapping) else None
    supplied_timeline = supplied.get("timeline")
    pr_number = _as_int(supplied.get("pr_number"))
    candidates: set[int] = {pr_number} if pr_number else set()
    candidate_sources: dict[int, str] = {
        pr_number: "injected" for pr_number in candidates
    }
    if pr_data is not None:
        injected_number = _as_int(pr_data.get("number"))
        if injected_number is not None:
            candidates.add(injected_number)
            candidate_sources.setdefault(injected_number, "injected")
    for event in events:
        payload = _json_dict(event.get("payload"))
        number = _as_int(payload.get("pr_number") or payload.get("pull_request_number"))
        if number and (not payload.get("repository") or payload.get("repository") == repository):
            candidates.add(number)
            candidate_sources.setdefault(number, "task_event")

    fetched_at: Optional[int] = _as_int(supplied.get("fetched_at"))
    issue_failed = False
    if issue_data is None:
        try:
            fetched_issue = fetch(f"/repos/{repository}/issues/{issue}")
            if isinstance(fetched_issue, Mapping):
                issue_data = fetched_issue
                fetched_at = fetched_at if fetched_at is not None else generated_at
            else:
                issue_failed = True
        except (OSError, HTTPError, URLError, ValueError, TypeError, RuntimeError):
            issue_failed = True
    if issue_data is None:
        return _github_unavailable_result(
            repository, issue, fetched_at=fetched_at,
            issue_data=None,
            error="github_issue_unavailable" if issue_failed else "github_issue_missing",
        )

    issue_repo = None
    issue_repository = issue_data.get("repository")
    if isinstance(issue_repository, Mapping):
        issue_repo = issue_repository.get("full_name")
    issue_repo = issue_repo or issue_data.get("full_name")
    repository_anchor = issue_repo in (None, repository)

    direct_closure_available = False
    if pr_data is not None:
        _direct_matches, direct_closure_available = _has_closing_reference(
            pr_data, repository=repository, issue=issue
        )

    timeline_failed = False
    if supplied_timeline is not None:
        timeline = supplied_timeline
    elif direct_closure_available:
        timeline = None
    else:
        try:
            timeline = fetch(
                f"/repos/{repository}/issues/{issue}/timeline?per_page=100"
            )
            fetched_at = fetched_at if fetched_at is not None else generated_at
        except (OSError, HTTPError, URLError, ValueError, TypeError, RuntimeError):
            timeline = None
            timeline_failed = True
    for number in _timeline_pr_numbers(timeline):
        candidates.add(number)
        candidate_sources.setdefault(number, "issue_timeline")

    graph_fetcher = github_graphql_fetcher
    if graph_fetcher is None and github_fetcher is None:
        graph_fetcher = _default_github_graphql_fetch
    selected_pr: Optional[Mapping[str, Any]] = None
    selected_graphql: Optional[Mapping[str, Any]] = None
    selected_number: Optional[int] = None
    selected_source: Optional[str] = None
    closure_unavailable = False
    for candidate in sorted(candidates):
        candidate_data = (
            pr_data
            if pr_data is not None and _as_int(pr_data.get("number")) == candidate
            else None
        )
        if candidate_data is None:
            try:
                fetched_pr = fetch(f"/repos/{repository}/pulls/{candidate}")
                if isinstance(fetched_pr, Mapping):
                    candidate_data = fetched_pr
                    fetched_at = fetched_at if fetched_at is not None else generated_at
            except (OSError, HTTPError, URLError, ValueError, TypeError, RuntimeError):
                continue
        if candidate_data is None:
            continue

        matches, closure_available = _has_closing_reference(
            candidate_data, repository=repository, issue=issue
        )
        candidate_graphql: Optional[Mapping[str, Any]] = None
        if not closure_available and graph_fetcher is not None:
            try:
                candidate_graphql = _graphql_pull_request(
                    graph_fetcher, repository, candidate
                )
                matches, closure_available = _has_closing_reference(
                    candidate_graphql, repository=repository, issue=issue
                )
            except (OSError, HTTPError, URLError, ValueError, TypeError, RuntimeError):
                closure_unavailable = True
                continue
        if not closure_available:
            closure_unavailable = True
            continue
        if not matches:
            continue
        selected_pr = candidate_data
        selected_graphql = candidate_graphql
        selected_number = candidate
        selected_source = candidate_sources.get(candidate, "candidate")
        break

    issue_state = str(issue_data.get("state") or "").lower() or None
    if selected_pr is None:
        error = (
            "github_pr_closing_reference_unavailable"
            if closure_unavailable or candidates
            else "github_pr_unavailable"
        )
        result = _github_unavailable_result(
            repository, issue, fetched_at=fetched_at or generated_at,
            issue_data=issue_data, error=error,
        )
        result["repository_anchor_verified"] = repository_anchor
        result["issue_closure_authoritative"] = (
            repository_anchor and issue_state == "closed"
        )
        return result

    base = selected_pr.get("base")
    pr_repo = None
    if isinstance(base, Mapping):
        base_repo = base.get("repo")
        if isinstance(base_repo, Mapping):
            pr_repo = base_repo.get("full_name")
    if pr_repo is None and isinstance(selected_pr.get("repository"), Mapping):
        pr_repo = selected_pr["repository"].get("full_name")
    if pr_repo is None and isinstance(selected_graphql, Mapping):
        graphql_repo = selected_graphql.get("repository")
        if isinstance(graphql_repo, Mapping):
            pr_repo = graphql_repo.get("nameWithOwner") or graphql_repo.get("full_name")
    anchor_ok = repository_anchor and pr_repo in (None, repository)
    pr_state = str(selected_pr.get("state") or "").lower() or None
    merged_at = selected_pr.get("merged_at")
    if merged_at is None and isinstance(selected_graphql, Mapping):
        merged_at = selected_graphql.get("mergedAt")
    head = selected_pr.get("head")
    observed_head = head.get("sha") if isinstance(head, Mapping) else None
    if observed_head is None and isinstance(selected_graphql, Mapping):
        observed_head = selected_graphql.get("headRefOid")
    merge_commit = selected_pr.get("merge_commit_sha")
    if merge_commit is None and isinstance(selected_graphql, Mapping):
        merge_node = selected_graphql.get("mergeCommit")
        merge_commit = merge_node.get("oid") if isinstance(merge_node, Mapping) else None
    return {
        "availability": "known" if anchor_ok and not timeline_failed else "partial",
        "fetched_at": fetched_at or generated_at,
        "repository": repository,
        "repository_anchor_verified": anchor_ok,
        "issue_number": issue,
        "issue_state": issue_state,
        "issue_closed": issue_state == "closed" if issue_state else None,
        "issue_closed_at": issue_data.get("closed_at"),
        "pr_number": selected_number or _as_int(selected_pr.get("number")),
        "pr_state": pr_state,
        "pr_merged_at": merged_at,
        "observed_pr_head_sha": _sha_or_none(observed_head),
        "merge_commit_sha": _sha_or_none(merge_commit),
        "outcome": "merged" if merged_at else ("open" if pr_state == "open" else pr_state),
        "issue_closure_authoritative": anchor_ok and issue_state == "closed",
        "closing_reference_verified": True,
        "pr_discovery": selected_source,
    }


def _timing_report(tasks: Mapping[str, Mapping[str, Any]], runs: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    starts = [_as_int(task.get("created_at")) for task in tasks.values()]
    starts.extend(_as_int(run.get("started_at")) for run in runs)
    ends = [_as_int(task.get("completed_at")) for task in tasks.values()]
    ends.extend(_as_int(run.get("ended_at")) for run in runs)
    starts = [value for value in starts if value is not None]
    ends = [value for value in ends if value is not None]
    first = min(starts) if starts else None
    last = max(ends) if ends else None
    elapsed = last - first if first is not None and last is not None and last >= first else None
    worker_by_profile: dict[str, int] = defaultdict(int)
    worker_known = 0
    for run in runs:
        started, ended = _as_int(run.get("started_at")), _as_int(run.get("ended_at"))
        if started is None or ended is None or ended < started:
            continue
        worker_known += 1
        worker_by_profile[str(run.get("profile") or "unknown")] += ended - started
    dependency_wait = _paired_wait_seconds(
        events,
        start_predicate=lambda event: str(event.get("kind") or "") in {"dependency_wait", "blocked"} and _event_is_dependency_block(event),
        end_predicate=lambda event: str(event.get("kind") or "") in {"unblocked", "promoted", "claimed", "dependency_resolved", "ready"},
    )
    operator_wait = _paired_wait_seconds(
        events,
        start_predicate=_event_is_operator_block,
        end_predicate=lambda event: str(event.get("kind") or "") in {"unblocked", "github_blocked_resolved", "block_resolved"},
    )
    return {
        "trajectory_start": first,
        "trajectory_end": last,
        "trajectory_elapsed_seconds": elapsed,
        "trajectory_elapsed_hours": elapsed / 3600 if elapsed is not None else None,
        "trajectory_elapsed_availability": "known" if elapsed is not None else "unavailable",
        "worker_run_seconds_by_profile": dict(sorted(worker_by_profile.items())),
        "summed_worker_seconds": sum(worker_by_profile.values()) if worker_known else None,
        "summed_worker_hours": sum(worker_by_profile.values()) / 3600 if worker_known else None,
        "worker_run_duration_availability": "known" if worker_known == len(runs) and runs else ("partial" if worker_known else "unavailable"),
        "dependency_wait_seconds": dependency_wait,
        "dependency_wait_availability": "known" if dependency_wait is not None else "unknown",
        "operator_wait_seconds": operator_wait,
        "operator_wait_availability": "known" if operator_wait is not None else "unknown",
    }


def _sha_or_none(value: Any) -> Optional[str]:
    value = str(value or "").strip()
    return value if _SHA_RE.fullmatch(value) else None


def _counts_report(
    tasks: Mapping[str, Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    roles: Mapping[str, str],
) -> dict[str, Any]:
    runs_by_task: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for run in runs:
        runs_by_task[str(run.get("task_id"))].append(run)
    role_counts = Counter(roles.values())
    outcomes = Counter(str(run.get("outcome") or run.get("status") or "unknown") for run in runs)
    reviewer_results: dict[str, str] = {}
    reviewer_completed: list[Mapping[str, Any]] = []
    for task_id, role in roles.items():
        if role != "reviewer":
            continue
        completed = [
            run for run in runs_by_task.get(task_id, [])
            if str(run.get("outcome") or "").casefold() == "completed"
            or str(run.get("status") or "").casefold() == "done"
        ]
        if completed:
            completed.sort(key=lambda run: (_as_int(run.get("ended_at")) or 0, _as_int(run.get("id")) or 0))
            chosen = completed[-1]
            reviewer_completed.append(chosen)
            reviewer_results[task_id] = _trusted_verdict(chosen) or "UNKNOWN"
        else:
            reviewer_results[task_id] = "UNKNOWN"
    verdicts = Counter(reviewer_results.values())
    reviewer_completed.sort(key=lambda run: (_as_int(run.get("ended_at")) or 0, _as_int(run.get("id")) or 0))
    first_verdict = _trusted_verdict(reviewer_completed[0]) if reviewer_completed else None

    payloads_by_task: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        payloads_by_task[str(event.get("task_id"))].append(_json_dict(event.get("payload")))
    confirmed_refresh = 0
    for task_id, role in roles.items():
        if role == "investigator" and any(
            _has_model_refresh_marker(run, payloads_by_task.get(task_id, []))
            for run in runs_by_task.get(task_id, [])
        ):
            confirmed_refresh += 1

    operator_pairs = 0
    outstanding = 0
    ordered_events = sorted(events, key=lambda event: (_as_int(event.get("created_at")) or 0, _as_int(event.get("id")) or 0))
    for event in ordered_events:
        if _event_is_operator_block(event):
            outstanding += 1
        elif outstanding and str(event.get("kind") or "") in {"unblocked", "github_blocked_resolved", "block_resolved"}:
            outstanding -= 1
            operator_pairs += 1

    implementation_rounds = sum(
        role == "developer" and any(_as_int(run.get("started_at")) is not None for run in runs_by_task.get(task_id, []))
        for task_id, role in roles.items()
    )
    archived_unrun_ids = [
        task_id for task_id, task in tasks.items()
        if roles.get(task_id) == "developer"
        and str(task.get("status") or "") == "archived"
        and not runs_by_task.get(task_id)
    ]
    infra_retry_count = sum(
        str(run.get("outcome") or "").casefold() in {"crashed", "timed_out", "failed", "spawn_failed", "reclaimed"}
        for run in runs
    )
    return {
        "specialist_tasks": len(tasks),
        "task_roles": {
            "developer_total": role_counts.get("developer", 0),
            "developer_work_rounds": implementation_rounds,
            "developer_work_rounds_excluding_archived_unrun_canary": implementation_rounds,
            "reviewer_rounds": role_counts.get("reviewer", 0),
            "investigator_rounds": role_counts.get("investigator", 0),
            "investigator_runs": role_counts.get("investigator", 0),
            "designer_rounds": role_counts.get("designer", 0),
        },
        "task_runs": len(runs),
        "run_outcomes": dict(sorted(outcomes.items())),
        "reviewer_verdicts": {
            "PASS": verdicts.get("PASS", 0),
            "REWORK": verdicts.get("REWORK", 0),
            "UNKNOWN": verdicts.get("UNKNOWN", 0),
        },
        "reviewer_rework_count": verdicts.get("REWORK", 0),
        "first_pass_success": {
            "value": True if first_verdict == "PASS" else (False if first_verdict == "REWORK" else None),
            "status": first_verdict or "UNKNOWN",
            "availability": "known" if first_verdict in {"PASS", "REWORK"} else "unknown",
        },
        "implementation_review_rounds": {"implementation": implementation_rounds, "review": role_counts.get("reviewer", 0)},
        "infrastructure_retries": {
            "value": infra_retry_count,
            "count": infra_retry_count,
            "availability": "known",
            "definition": "terminal crash/timeout/spawn/runtime failure runs only",
        },
        "operator_intervention": {
            "block_unblock_pairs": operator_pairs,
            "operator_block_unblock_pairs": operator_pairs,
            "pair_duration_seconds": _paired_wait_seconds(
                events,
                start_predicate=_event_is_operator_block,
                end_predicate=lambda event: str(event.get("kind") or "") in {"unblocked", "github_blocked_resolved", "block_resolved"},
            ),
            "physical_human_count": None,
            "physical_human_availability": "unknown",
        },
        "investigation_model_refresh": {
            "candidate_count_after_initial": max(0, role_counts.get("investigator", 0) - 1),
            "candidates_after_initial": max(0, role_counts.get("investigator", 0) - 1),
            "confirmed_count": confirmed_refresh if confirmed_refresh else None,
            "confirmed_availability": "known" if confirmed_refresh else "unknown",
        },
        "archived_unrun_canary": {
            "task_id": archived_unrun_ids[0] if archived_unrun_ids else None,
            "count": len(archived_unrun_ids),
        },
    }


def _project_identity(tasks: Mapping[str, Mapping[str, Any]], runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    runs_by_task: dict[str, set[str]] = defaultdict(set)
    for run in runs:
        runs_by_task[str(run.get("task_id"))].add(str(run.get("profile") or "unknown"))
    scoped: set[tuple[str, str, str]] = set()
    null_count = 0
    for task_id, task in tasks.items():
        project_id = task.get("project_id")
        if project_id in (None, ""):
            null_count += 1
            continue
        profiles = runs_by_task.get(task_id) or {"unknown"}
        for profile in profiles:
            scoped.add((str(project_id), profile, str(task.get("created_by") or "unknown")))
    return {
        "scoped_project_ids": [
            {"project_id": project_id, "profile": profile, "creator": creator}
            for project_id, profile, creator in sorted(scoped)
        ],
        "null_project_id_task_count": null_count,
        "join_rule": "project_id is profile/creator scoped and is not repository identity",
    }


def _ordered_evidence(
    tasks: Mapping[str, Mapping[str, Any]],
    links: Sequence[Mapping[str, str]],
    runs: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    roles: Mapping[str, str],
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for task_id, task in tasks.items():
        evidence.append({"source": "kanban.tasks", "row_id": task_id, "task_id": task_id, "kind": "task", "role": roles.get(task_id, "other"), "at": _as_int(task.get("created_at"))})
    selected = set(tasks)
    for link in links:
        if link["parent_id"] in selected and link["child_id"] in selected:
            evidence.append({"source": "kanban.task_links", "row_id": f"{link['parent_id']}->{link['child_id']}", "parent_id": link["parent_id"], "child_id": link["child_id"], "kind": "link"})
    for run in runs:
        metadata = _run_metadata(run)
        evidence.append({
            "source": "kanban.task_runs", "row_id": _as_int(run.get("id")), "task_id": str(run.get("task_id")),
            "kind": "run", "profile": str(run.get("profile") or "unknown"),
            "status": str(run.get("status") or "unknown"), "outcome": str(run.get("outcome") or "unknown"),
            "started_at": _as_int(run.get("started_at")), "ended_at": _as_int(run.get("ended_at")),
            "worker_session_id": _safe_id(metadata.get("worker_session_id")),
        })
    for event in events:
        evidence.append({
            "source": "kanban.task_events", "row_id": _as_int(event.get("id")),
            "task_id": str(event.get("task_id")), "run_id": _as_int(event.get("run_id")),
            "kind": str(event.get("kind") or "unknown"), "at": _as_int(event.get("created_at")),
            "payload_available": bool(event.get("payload")),
        })
    evidence.sort(key=lambda item: (_as_int(item.get("at") or item.get("started_at")) or 0, str(item.get("source")), str(item.get("row_id"))))
    return evidence


def _human_summary(
    repository: str, issue: int, counts: Mapping[str, Any], github: Mapping[str, Any],
    usage: Optional[Mapping[str, Any]] = None, timing: Optional[Mapping[str, Any]] = None,
) -> str:
    roles = counts.get("task_roles") or {}
    usage_totals = (usage or {}).get("totals") or {}
    total_tokens = usage_totals.get("total_tokens")
    worker_seconds = (timing or {}).get("summed_worker_seconds")
    return (
        f"Issue #{issue} in {repository}: {counts.get('specialist_tasks', 0)} specialist tasks, "
        f"{roles.get('developer_work_rounds', 0)} implementation rounds and "
        f"{roles.get('reviewer_rounds', 0)} review rounds, "
        f"rework={counts.get('reviewer_rework_count', 0)}, "
        f"tokens={total_tokens if total_tokens is not None else 'unavailable'}, "
        f"worker_seconds={worker_seconds if worker_seconds is not None else 'unavailable'}; "
        f"fresh GitHub outcome={github.get('outcome') or 'unavailable'}."
    )


def build_trajectory_report(
    db_path: Path | str,
    *,
    board_slug: str,
    repository: str,
    issue: int,
    root_task_id: Optional[str] = None,
    task_ids: Optional[Sequence[str]] = None,
    profile_root: Optional[Path | str] = None,
    github_evidence: Optional[Mapping[str, Any]] = None,
    github_fetcher: Optional[Callable[[str], Any]] = None,
    github_graphql_fetcher: Optional[Callable[[str, Mapping[str, Any]], Mapping[str, Any]]] = None,
    generated_at: Optional[int] = None,
) -> dict[str, Any]:
    """Build one observer-only report from durable source rows."""
    repository = _validate_repository(repository)
    issue = _as_int(issue) or 0
    if issue <= 0:
        raise TrajectoryInputError("issue must be a positive integer")
    generated = _now(generated_at)
    path = Path(db_path)
    root_id: Optional[str] = None
    root_status = "unavailable"
    root: Optional[dict[str, Any]] = None
    tasks: dict[str, dict[str, Any]] = {}
    links: list[dict[str, str]] = []
    runs: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    table_availability = {name: "unavailable" for name in ("tasks", "task_links", "task_runs", "task_events")}
    provenance: dict[str, Any] = {}
    kanban_error: Optional[str] = None
    try:
        conn = _read_only_connection(path)
        try:
            all_tasks = _select_task_rows(conn)
            links = _select_links(conn)
            table_availability = {
                name: "known" if _table_columns(conn, name) else "unavailable"
                for name in ("tasks", "task_links", "task_runs", "task_events")
            }
            root_id, root_status, root, tasks, provenance = _select_scope(
                all_tasks, links, repository=repository, issue=issue,
                root_task_id=root_task_id, task_ids=task_ids,
            )
            runs = _select_runs(conn, list(tasks))
            events = _select_events(conn, list(tasks))
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        kanban_error = "kanban_db_unavailable"
        provenance = {
            "method": "unavailable", "root_task_status": "unavailable",
            "selected_task_ids_explicit": task_ids is not None, "selected_task_count": 0,
            "body_or_title_matching": False,
        }

    runs_by_task: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for run in runs:
        runs_by_task[str(run.get("task_id"))].append(run)
    roles = {task_id: _role(task, runs_by_task.get(task_id, [])) for task_id, task in tasks.items()}
    counts = _counts_report(tasks, runs, events, roles)
    counts["task_links"] = sum(
        link["parent_id"] in tasks and link["child_id"] in tasks for link in links
    )
    timing = _timing_report(tasks, runs, events)
    usage, usage_records = _usage_report(
        runs, events, profile_root=Path(profile_root) if profile_root is not None else None,
    )
    github = _github_outcome(
        repository, issue, events, github_evidence=github_evidence,
        github_fetcher=github_fetcher, github_graphql_fetcher=github_graphql_fetcher,
        generated_at=generated,
    )
    root_row = None
    if root is not None:
        root_row = {
            "id": str(root.get("id")),
            "status": str(root.get("status") or "unknown"),
            "created_at": _as_int(root.get("created_at")),
            "completed_at": _as_int(root.get("completed_at")),
            "project_id": root.get("project_id"),
            "idempotency_key_verified": root.get("idempotency_key") == f"github:{repository}:issue:{issue}",
        }
    identity = {
        "repository": repository,
        "issue_number": issue,
        "repository_anchor": {
            "kind": "github_repository", "value": repository,
            "verified": github.get("repository_anchor_verified") is True,
            "availability": "known" if github.get("repository_anchor_verified") is True else "partial",
        },
        "root_task_id": root_id,
        "root_task_row": root_row,
        "root_task_status": root_status,
        "selection": provenance,
        "project_identity": _project_identity(tasks, runs),
    }
    diagnostics: list[str] = []
    if kanban_error:
        diagnostics.append(kanban_error)
    if root_status != "known":
        diagnostics.append("root_task_identity_" + root_status)
    if any(record.get("reason") == "worker_session_id_missing" for record in usage_records):
        diagnostics.append("worker_session_id_unavailable_for_nonterminal_runs")
    if usage["tool_failures"]["availability"] == "unavailable":
        diagnostics.append("tool_failure_retry_telemetry_unavailable")
    if counts["investigation_model_refresh"]["confirmed_availability"] != "known":
        diagnostics.append("investigation_model_refresh_causal_marker_unavailable")
    if github.get("availability") != "known":
        diagnostics.append("github_final_outcome_" + str(github.get("availability")))
    status = (
        "complete"
        if root_status == "known"
        and github.get("availability") == "known"
        and all(availability == "known" for availability in table_availability.values())
        else "partial"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "schema_id": SCHEMA_ID,
        "generated_at": generated,
        "freshness": {
            "report_generated_at": generated,
            "kanban_observed_at": generated if not kanban_error else None,
            "profile_state_observed_at": generated if usage["availability"] != "unavailable" else None,
            "github_fetched_at": github.get("fetched_at"),
        },
        "read_only": True,
        "status": status,
        "identity": identity,
        "source": {
            "board_slug": str(board_slug or "default"),
            "kanban": {"availability": "unavailable" if kanban_error else "known", "read_only": True, "tables": table_availability, "error": kanban_error},
            "profile_state": {"availability": usage["availability"], "read_only": True, "profiles_observed": sorted(usage["by_profile"])},
            "github": {"availability": github.get("availability"), "fetched_at": github.get("fetched_at"), "read_only": True},
        },
        "counts": counts,
        "timing": timing,
        "usage": usage,
        "github": github,
        "closure": {
            "internal_root_terminal": root is not None and str(root.get("status") or "") in {"done", "archived"},
            "github_pr_merged": github.get("pr_merged_at") is not None,
            "github_issue_closed": github.get("issue_closed") is True,
            "authoritative": github.get("issue_closure_authoritative") is True,
            "source": "github_edge_outcome",
        },
        "evidence": _ordered_evidence(tasks, links, runs, events, roles),
        "coverage": {
            "field_availability": {
                "identity": "known" if root_status == "known" else root_status,
                "trajectory": "known" if tasks else "unavailable",
                "timing": timing["trajectory_elapsed_availability"],
                "usage": usage["availability"],
                "github": github.get("availability"),
            },
            "diagnostics": diagnostics + [
                f"kanban_table_{name}_unavailable"
                for name, availability in table_availability.items()
                if availability != "known"
            ],
        },
        "summary": _human_summary(repository, issue, counts, github, usage, timing),
    }


def discover_root_candidates(
    db_path: Path | str, *, repository: Optional[str] = None,
    issue: Optional[int] = None, from_epoch: Optional[int] = None,
    to_epoch: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Find exact GitHub issue roots for an optional repository/time scope."""
    if repository is not None:
        repository = _validate_repository(repository)
    issue_number = int(issue) if issue is not None else None
    try:
        conn = _read_only_connection(Path(db_path))
    except (OSError, sqlite3.Error):
        return []
    try:
        columns = _table_columns(conn, "tasks")
        if not {"id", "idempotency_key"}.issubset(columns):
            return []
        created = '"created_at"' if "created_at" in columns else "0"
        rows = conn.execute(
            f"SELECT id, idempotency_key, {created} AS created_at FROM tasks "
            "WHERE idempotency_key LIKE 'github:%' ORDER BY created_at, id"
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            match = re.fullmatch(r"github:([^:]+/[^:]+):issue:(\d+)", str(row["idempotency_key"] or ""))
            if not match:
                continue
            row_repository, row_issue = match.group(1), int(match.group(2))
            if repository is not None and row_repository != repository:
                continue
            if issue_number is not None and row_issue != issue_number:
                continue
            created_at = _as_int(row["created_at"]) or 0
            if from_epoch is not None and created_at < int(from_epoch):
                continue
            if to_epoch is not None and created_at > int(to_epoch):
                continue
            results.append({"root_task_id": str(row["id"]), "repository": row_repository, "issue": row_issue, "created_at": created_at})
        return results
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def discover_root_task_ids(
    db_path: Path | str, *, repository: str,
    from_epoch: Optional[int] = None, to_epoch: Optional[int] = None,
) -> list[tuple[str, int, int]]:
    """Find roots by exact idempotency keys, never title/body text."""
    repository = _validate_repository(repository)
    prefix = f"github:{repository}:issue:"
    try:
        conn = _read_only_connection(Path(db_path))
    except (OSError, sqlite3.Error):
        return []
    try:
        columns = _table_columns(conn, "tasks")
        if not {"id", "idempotency_key"}.issubset(columns):
            return []
        created = '"created_at"' if "created_at" in columns else "0"
        rows = conn.execute(
            f"SELECT id, idempotency_key, {created} AS created_at FROM tasks "
            "WHERE idempotency_key LIKE ? ORDER BY created_at, id", (prefix + "%",)
        ).fetchall()
        results: list[tuple[str, int, int]] = []
        for row in rows:
            match = re.fullmatch(re.escape(prefix) + r"(\d+)", str(row["idempotency_key"] or ""))
            if not match:
                continue
            created_at = _as_int(row["created_at"]) or 0
            if from_epoch is not None and created_at < int(from_epoch):
                continue
            if to_epoch is not None and created_at > int(to_epoch):
                continue
            results.append((str(row["id"]), int(match.group(1)), created_at))
        return results
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def aggregate_trajectory_reports(
    reports: Sequence[Mapping[str, Any]], *,
    from_epoch: Optional[int] = None, to_epoch: Optional[int] = None,
) -> dict[str, Any]:
    """Aggregate reports with known-only totals and explicit denominators."""
    selected = []
    for report in reports:
        created = _as_int(((report.get("identity") or {}).get("root_task_row") or {}).get("created_at"))
        if from_epoch is not None and created is not None and created < int(from_epoch):
            continue
        if to_epoch is not None and created is not None and created > int(to_epoch):
            continue
        selected.append(report)
    first_pass: list[bool] = []
    unknown_first = 0
    reworks: list[int] = []
    tokens: list[int] = []
    seconds: list[int] = []
    model_totals: dict[str, dict[str, Any]] = {}
    model_eligible = 0
    for report in selected:
        counts = report.get("counts") or {}
        first = counts.get("first_pass_success") or {}
        if first.get("availability") == "known" and isinstance(first.get("value"), bool):
            first_pass.append(bool(first["value"]))
        else:
            unknown_first += 1
        rework = _as_int(counts.get("reviewer_rework_count"))
        if rework is not None:
            reworks.append(rework)
        total = _as_int(((report.get("usage") or {}).get("totals") or {}).get("total_tokens"))
        if total is not None:
            tokens.append(total)
        worker = _as_int((report.get("timing") or {}).get("summed_worker_seconds"))
        if worker is not None:
            seconds.append(worker)
        model_rows = (report.get("usage") or {}).get("by_effective_model") or {}
        if any(
            _as_int(value.get("total_tokens")) is not None
            and (value.get("field_availability") or {}).get("total_tokens") == "known"
            for value in model_rows.values()
        ):
            model_eligible += 1
        for key, value in model_rows.items():
            bucket = model_totals.setdefault(
                key,
                {
                    "provider": value.get("provider"),
                    "model": value.get("model"),
                    "total_tokens": None,
                    "total_tokens_input_plus_output": None,
                    "known_reports": 0,
                    "reports": 0,
                    "availability": "unavailable",
                },
            )
            value_total = _as_int(value.get("total_tokens"))
            value_availability = (value.get("field_availability") or {}).get("total_tokens")
            # Numeric totals are eligible only when the source marks them known.
            if value_total is not None and value_availability in {None, "known"}:
                previous_total = bucket["total_tokens"]
                bucket["total_tokens"] = (
                    value_total
                    if previous_total is None else previous_total + value_total
                )
                bucket["known_reports"] += 1
            bucket["reports"] += 1
            bucket["total_tokens_input_plus_output"] = bucket["total_tokens"]
            known_reports = bucket["known_reports"]
            reports = bucket["reports"]
            bucket["availability"] = (
                "known" if known_reports == reports and reports else
                "partial" if known_reports else "unavailable"
            )
    generated = int(time.time())
    return {
        "schema_version": SCHEMA_VERSION,
        "schema_id": SCHEMA_ID,
        "read_only": True,
        "generated_at": generated,
        "freshness": {"report_generated_at": generated},
        "from": from_epoch,
        "to": to_epoch,
        "report_count": len(selected),
        "eligible_denominators": {"first_pass_success": len(first_pass), "average_rework": len(reworks), "token_efficiency": min(len(tokens), len(seconds)), "model_provider_comparison": model_eligible},
        "first_pass_success": {"successes": sum(first_pass), "eligible": len(first_pass), "rate": sum(first_pass) / len(first_pass) if first_pass else None, "unknown": unknown_first},
        "average_rework": {"value": sum(reworks) / len(reworks) if reworks else None, "eligible": len(reworks), "unknown": len(selected) - len(reworks)},
        "token_efficiency": {"total_tokens": sum(tokens) if tokens else None, "worker_seconds": sum(seconds) if seconds else None, "tokens_per_worker_second": sum(tokens) / sum(seconds) if tokens and seconds and sum(seconds) else None, "availability": "known" if tokens and seconds else "partial"},
        "by_effective_model": dict(sorted(model_totals.items())),
        "model_provider_comparison": dict(sorted(model_totals.items())),
        "unknown_counts": {"first_pass_success": unknown_first, "reports_without_token_total": len(selected) - len(tokens), "reports_without_worker_seconds": len(selected) - len(seconds), "model_provider_comparison": len(selected) - model_eligible},
        "reports": [{"repository": (report.get("identity") or {}).get("repository"), "issue_number": (report.get("identity") or {}).get("issue_number"), "status": report.get("status"), "summary": report.get("summary")} for report in selected],
    }