#!/usr/bin/env python3
"""Creation-time workspace graph preflight for specialist Kanban cards.

The stable lifecycle hook runs before Hermes' structured ``kanban_create``
handler.  Hermes core remains the canonical task/graph owner: this policy only
admits exact specialist creation requests, serializes same-idempotency-key
materialization, asks the existing ``kanban_db.create_task`` owner to create
(or return) the row, and immediately verifies the durable binding and links
inside the same outer core transaction.  A newly created mismatch therefore
rolls back instead of exposing a claimable row.

A project-linked worktree is the only implementation/rework shape admitted:
Hermes derives ``<project primary repo>/.worktrees/<task-id>`` and the
project-specific ``<slug>/<task-id>[-<title>]`` branch inside its normal
creation transaction.  Invalid input is rejected before opening the board DB,
so the rejected request cannot leave a malformed row behind.  Explicit
``initial_status=blocked`` remains the non-dispatchable operator/quarantine
escape hatch; the existing specialist completion and spawn-time workspace
policies remain separate defenses.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

SPECIALIST_ASSIGNEES = frozenset(
    {
        "kanban-investigator",
        "kanban-developer",
        "kanban-reviewer",
        "kanban-designer",
    }
)

_TASK_ID_RE = re.compile(r"^t_[A-Za-z0-9_-]+$")
_BRANCH_SAFE_RE = re.compile(r"[^a-z0-9._-]+")
_SHELL_SUBSTITUTION_RE = re.compile(r"[$`]|[<>]\(")
_LOCK_TIMEOUT_SECONDS = 8.0


class BindingError(RuntimeError):
    """The request or the durable read-back cannot prove safe admission."""


class _TaskAdapter(Protocol):
    """Small seam around the existing Hermes task/DB owner.

    Keeping the seam narrow makes the policy deterministic to test without
    reimplementing Hermes' schema or creating a second task store.
    """

    def find_idempotent(self, key: str) -> str | None: ...

    def create(self, raw: Mapping[str, Any], binding: "RepoBinding") -> str: ...

    def read(self, task_id: str) -> Mapping[str, Any] | None: ...

    def links(self, task_id: str) -> list[tuple[str, str]]: ...

    def parent_statuses(self, parent_ids: tuple[str, ...]) -> dict[str, str]: ...

    def transaction(self) -> Any: ...

    def quarantine(self, task_id: str, reason: str) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class RepoBinding:
    project_id: str
    project_slug: str
    anchor: Path


@dataclass(frozen=True)
class _ProjectRow:
    project_id: str
    project_slug: str
    primary_path: str
    archived: bool


def _exact_idempotency_key(value: Any) -> str:
    """Require the preflight and normal handler to use one byte-exact key."""
    if not isinstance(value, str) or not value.strip():
        raise BindingError(
            "idempotency_key is required to bind concurrent same-Issue/rework requests"
        )
    if value != value.strip():
        raise BindingError("idempotency_key must not have leading or trailing whitespace")
    return value


def _reject_unresolved_shell_substitutions(args: list[str]) -> None:
    """Do not materialize parser tokens whose shell meaning is unresolved.

    The specialist parser uses ``shlex`` intentionally without shell expansion,
    so a token such as ``$KEY`` would otherwise be persisted literally before
    the real shell expands it for the normal handler.  Reject all common
    parameter/command/arithmetic, backtick, and process-substitution markers
    across the complete parsed create argv and board selector, including the
    assignee itself.
    """
    if any(_SHELL_SUBSTITUTION_RE.search(value) for value in args):
        raise BindingError(
            "terminal specialist create arguments must not contain unresolved shell substitution tokens"
        )


def _log_path() -> Path:
    return Path(
        os.environ.get(
            "KANBAN_WORKSPACE_BINDING_GUARD_LOG",
            "/home/hermes/.hermes/kanban/logs/workspace-binding-guard.log",
        )
    )


def _log(entry: Mapping[str, Any]) -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(entry), ensure_ascii=False) + "\n")
    except OSError:
        pass


def _block(message: str, *, assignee: str = "", source: str = "kanban_create") -> int:
    diagnostic = (
        "H4V3 workspace-binding preflight failed closed: "
        f"{message}. No dispatchable task mutation was allowed."
    )
    print(json.dumps({"action": "block", "message": diagnostic}, ensure_ascii=False))
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


def _is_blocked_quarantine(raw: Mapping[str, Any]) -> bool:
    """Explicit blocked cards remain outside ready/claimable admission.

    The existing completion guard still rejects ``parents + blocked`` as a
    dependency shortcut.  This exception is only for a card explicitly parked
    by its creator for an operator/safety decision.
    """
    value = raw.get("initial_status")
    return isinstance(value, str) and value.strip().casefold() == "blocked"


def _as_bool(value: Any, *, field: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes"}:
        return True
    if isinstance(value, str) and value.strip().casefold() in {"false", "0", "no"}:
        return False
    raise BindingError(f"{field} must be a boolean")


def _as_optional_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BindingError(f"{field} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BindingError(f"{field} must be an integer") from exc


def _parents(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("parents")
    if value is None:
        return ()
    if isinstance(value, str):
        values: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise BindingError("parents must be a list of task ids")
    result: list[str] = []
    for parent in values:
        if not isinstance(parent, str) or not parent.strip():
            raise BindingError("parents must contain non-empty task ids")
        normalized = parent.strip()
        if normalized in result:
            raise BindingError("parents must not contain duplicate task ids")
        result.append(normalized)
    return tuple(result)


def _project_db_path() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "projects.db"


def _project_row(token: str) -> _ProjectRow:
    path = _project_db_path()
    if not path.is_file():
        raise BindingError(f"project catalog is missing or unreadable: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(projects)")}
            required = {"id", "slug", "primary_path"}
            if not required.issubset(columns):
                raise BindingError("project catalog has no verifiable primary repository field")
            archived_expr = ", archived" if "archived" in columns else ""
            row = conn.execute(
                f"SELECT id, slug, primary_path{archived_expr} FROM projects "
                "WHERE id = ? OR slug = ? LIMIT 1",
                (token, token.casefold()),
            ).fetchone()
        finally:
            conn.close()
    except BindingError:
        raise
    except sqlite3.Error as exc:
        raise BindingError(f"project catalog could not be read: {type(exc).__name__}") from exc
    if row is None:
        raise BindingError(f"project {token!r} is not registered")
    primary = str(row["primary_path"] or "").strip()
    return _ProjectRow(
        project_id=str(row["id"]),
        project_slug=str(row["slug"] or "").strip().casefold(),
        primary_path=primary,
        archived=bool(row["archived"]) if "archived" in row.keys() else False,
    )


def _git_output(anchor: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(anchor), *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BindingError(f"repository anchor could not be verified: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise BindingError("project primary path is not a readable git repository")
    return (result.stdout or "").strip()


def _resolve_binding(raw: Mapping[str, Any]) -> RepoBinding:
    kind = raw.get("workspace_kind")
    if not isinstance(kind, str) or kind.strip() != "worktree":
        raise BindingError("workspace_kind must be explicitly 'worktree'")
    if "workspace_path" in raw and raw.get("workspace_path") is not None:
        raise BindingError(
            "workspace_path must be omitted so the core materializer can derive "
            "<repo>/.worktrees/<task-id>"
        )
    if "workspace_path" in raw:
        raise BindingError("null workspace_path is not a canonical creation binding")
    for field in ("branch", "branch_name"):
        if field in raw:
            raise BindingError(f"{field} cannot be supplied on the structured create path")
    _exact_idempotency_key(raw.get("idempotency_key"))
    project_value = raw.get("project") if "project" in raw else raw.get("project_id")
    if not isinstance(project_value, str) or not project_value.strip():
        raise BindingError("project is required to resolve a repository anchor")
    if "project" in raw and "project_id" in raw:
        alternate = raw.get("project_id")
        if alternate not in (None, project_value):
            raise BindingError("project and project_id disagree")
    project = _project_row(project_value.strip())
    if project.archived:
        raise BindingError("project is archived and cannot anchor new work")
    if not project.project_slug:
        raise BindingError("project has no canonical slug")
    if not project.primary_path:
        raise BindingError("project has no primary repository anchor")
    anchor = Path(project.primary_path).expanduser()
    if not anchor.is_absolute() or not anchor.is_dir():
        raise BindingError("project primary repository anchor is missing or not a directory")
    try:
        resolved = anchor.resolve(strict=True)
    except OSError as exc:
        raise BindingError("project primary repository anchor is unverifiable") from exc
    git_root = Path(_git_output(resolved, "rev-parse", "--show-toplevel")).resolve(strict=False)
    if git_root != resolved:
        raise BindingError("project primary path is not the repository anchor root")
    git_dir = Path(_git_output(resolved, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = (resolved / git_dir).resolve(strict=False)
    else:
        git_dir = git_dir.resolve(strict=False)
    common_dir = Path(_git_output(resolved, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = (resolved / common_dir).resolve(strict=False)
    else:
        common_dir = common_dir.resolve(strict=False)
    if git_dir != common_dir:
        raise BindingError("project primary path is a linked worktree, not a repository anchor")
    worktrees = resolved / ".worktrees"
    if worktrees.exists():
        if worktrees.is_symlink():
            raise BindingError("repository .worktrees path is a symlink and cannot be verified")
        if not worktrees.is_dir():
            raise BindingError("repository .worktrees path is not a directory")
    return RepoBinding(project.project_id, project.project_slug, resolved)


def _branch_name(binding: RepoBinding, task_id: str, title: str) -> str:
    title_slug = _BRANCH_SAFE_RE.sub("-", title.strip().lower()).strip("-")[:40].strip("-")
    base = f"{binding.project_slug}/{task_id}"
    return f"{base}-{title_slug}" if title_slug else base


def _value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _expected_status(raw: Mapping[str, Any], parent_statuses: Mapping[str, str]) -> str:
    if _as_bool(raw.get("triage"), field="triage"):
        return "triage"
    parents = _parents(raw)
    if any(parent_statuses.get(parent) != "done" for parent in parents):
        return "todo"
    return "ready"


def _verify_readback(
    adapter: _TaskAdapter,
    raw: Mapping[str, Any],
    binding: RepoBinding,
    task_id: str,
) -> None:
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise BindingError("materializer returned an invalid task id")
    row = adapter.read(task_id)
    if row is None:
        raise BindingError("created task disappeared before durable read-back")
    title = str(raw.get("title") or "").strip()
    assignee = _specialist(raw.get("assignee"))
    if str(_value(row, "title") or "").strip() != title:
        raise BindingError("durable title read-back does not match the create request")
    if _specialist(_value(row, "assignee")) != assignee:
        raise BindingError("durable assignee read-back does not match the create request")
    if str(_value(row, "workspace_kind") or "") != "worktree":
        raise BindingError("durable workspace_kind is not worktree")
    stored_path = str(_value(row, "workspace_path") or "")
    expected_path = binding.anchor / ".worktrees" / task_id
    if not stored_path or Path(stored_path).expanduser().resolve(strict=False) != expected_path.resolve(strict=False):
        raise BindingError("durable workspace_path is not <repo>/.worktrees/<task-id>")
    stored_branch = str(_value(row, "branch_name") or "")
    expected_branch = _branch_name(binding, task_id, title)
    if not stored_branch or stored_branch != expected_branch:
        raise BindingError("durable branch_name is not the dedicated project branch")
    if str(_value(row, "project_id") or "") != binding.project_id:
        raise BindingError("durable project binding does not match the resolved project")
    if str(_value(row, "idempotency_key") or "") != _exact_idempotency_key(
        raw["idempotency_key"]
    ):
        raise BindingError("durable idempotency key read-back does not match")
    parents = _parents(raw)
    actual_links = sorted(adapter.links(task_id))
    expected_links = sorted((parent, task_id) for parent in parents)
    if actual_links != expected_links:
        raise BindingError("durable parent topology is not the requested parent-to-child graph")
    parent_statuses = adapter.parent_statuses(parents)
    if set(parent_statuses) != set(parents):
        raise BindingError("durable parent topology contains an unreadable parent")


def _verify_initial_status(
    adapter: _TaskAdapter,
    raw: Mapping[str, Any],
    task_id: str,
) -> None:
    """Check creation placement only for a newly materialized task.

    Idempotent replay must validate the immutable binding/topology above while
    leaving the task's current lifecycle state alone.  A replay can legitimately
    observe a task after the dispatcher has claimed it or after edge lifecycle
    reconciliation has moved it to review/done.
    """
    row = adapter.read(task_id)
    if row is None:
        raise BindingError("created task disappeared before initial status read-back")
    parents = _parents(raw)
    parent_statuses = adapter.parent_statuses(parents)
    if set(parent_statuses) != set(parents):
        raise BindingError("durable parent topology contains an unreadable parent")
    expected_status = _expected_status(raw, parent_statuses)
    if str(_value(row, "status") or "") != expected_status:
        raise BindingError(f"durable status read-back is not {expected_status!r}")


def _board_db_path(board: str | None) -> Path:
    """Resolve the exact board path through Hermes' canonical resolver.

    The Kanban DB is shared across profiles.  Reimplementing the resolver here
    can silently put the creation lock beside a profile-local shadow database
    when ``HERMES_KANBAN_HOME``, the current-board pointer, or a case variant is
    in use.  Fail closed when the core resolver is unavailable instead of
    falling back to a path that core will not open.
    """
    try:
        from hermes_cli import kanban_db as kb  # pyright: ignore[reportMissingImports]

        path = kb.kanban_db_path(board=board)
        return Path(path).expanduser()
    except Exception as exc:
        raise BindingError(
            f"Hermes Kanban board resolver is unavailable: {type(exc).__name__}"
        ) from exc


@contextlib.contextmanager
def _creation_lock(board: str | None, key: str):
    """Serialize only the same board/idempotency materialization window."""
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - Linux is the supported host
        raise BindingError("same-round creation lock is unavailable") from exc
    db_path = _board_db_path(board)
    lock_root = db_path.parent
    try:
        lock_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        handle = (lock_root / f".workspace-binding-{digest}.lock").open("a+b")
    except OSError as exc:
        raise BindingError("same-round creation lock cannot be opened") from exc
    acquired = False
    try:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.02)
            except OSError as exc:
                raise BindingError("same-round creation lock failed") from exc
        if not acquired:
            raise BindingError("same-round creation lock was busy; retry the same idempotent request")
        yield db_path
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class _CoreTaskAdapter:
    """Adapter to Hermes' existing Kanban DB owner; no second store is used."""

    def __init__(self, board: str | None, *, db_path: Path | None = None):
        try:
            from hermes_cli import kanban_db as kb  # pyright: ignore[reportMissingImports]
            from hermes_cli import kanban_db_connect as kbc  # pyright: ignore[reportMissingImports]
            self._kb = kb
            self._kbc = kbc
            self._conn = kbc.connect(db_path=db_path, board=board)
        except Exception as exc:
            raise BindingError(
                f"Hermes Kanban materializer is unavailable: {type(exc).__name__}"
            ) from exc

    def find_idempotent(self, key: str) -> str | None:
        rows = self._conn.execute(
            "SELECT id, created_at FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC",
            (key,),
        ).fetchall()
        if not rows:
            return None
        newest_created_at = rows[0]["created_at"]
        if sum(row["created_at"] == newest_created_at for row in rows) > 1:
            raise BindingError(
                "same-key legacy rows have tied creation timestamps; replay is ambiguous"
            )
        return str(rows[0]["id"])

    def create(self, raw: Mapping[str, Any], binding: RepoBinding) -> str:
        parents = _parents(raw)
        return str(
            self._kb.create_task(
                self._conn,
                title=str(raw["title"]).strip(),
                body=raw.get("body"),
                assignee=str(raw["assignee"]),
                created_by=(str(raw["created_by"]) if raw.get("created_by") else None)
                or os.environ.get("HERMES_PROFILE")
                or "worker",
                workspace_kind="worktree",
                workspace_path=None,
                project_id=binding.project_id,
                tenant=raw.get("tenant") or os.environ.get("HERMES_TENANT"),
                priority=_as_optional_int(raw.get("priority"), field="priority") or 0,
                parents=parents,
                triage=_as_bool(raw.get("triage"), field="triage"),
                idempotency_key=_exact_idempotency_key(raw["idempotency_key"]),
                max_runtime_seconds=_as_optional_int(
                    raw.get("max_runtime_seconds"), field="max_runtime_seconds"
                ),
                skills=raw.get("skills"),
                max_retries=_as_optional_int(raw.get("max_retries"), field="max_retries"),
                model_override=raw.get("model"),
                provider_override=raw.get("provider"),
                goal_mode=_as_bool(raw.get("goal_mode"), field="goal_mode"),
                goal_max_turns=_as_optional_int(
                    raw.get("goal_max_turns"), field="goal_max_turns"
                ),
                initial_status=str(raw.get("initial_status") or "running"),
                session_id=raw.get("session_id") or os.environ.get("HERMES_SESSION_ID"),
                board=raw.get("board"),
                creator_task_id=os.environ.get("HERMES_KANBAN_TASK") or None,
                completion_contract=raw.get("completion_contract"),
            )
        )

    def read(self, task_id: str) -> Mapping[str, Any] | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row

    def links(self, task_id: str) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT parent_id, child_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        return [(str(row["parent_id"]), str(row["child_id"])) for row in rows]

    def parent_statuses(self, parent_ids: tuple[str, ...]) -> dict[str, str]:
        if not parent_ids:
            return {}
        placeholders = ",".join("?" * len(parent_ids))
        rows = self._conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({placeholders})", parent_ids
        ).fetchall()
        return {str(row["id"]): str(row["status"]) for row in rows}

    def transaction(self) -> Any:
        """Open the outer core transaction used by creation verification."""
        return self._kbc.write_txn(self._conn)

    def quarantine(self, task_id: str, reason: str) -> bool:
        """CAS-quarantine an existing unclaimed malformed row.

        This runs inside :meth:`transaction`; it deliberately never clears
        claim metadata or changes a running task.  A new row never takes this
        path: its verification exception rolls the outer transaction back.
        """
        columns = {str(row[1]) for row in self._conn.execute("PRAGMA table_info(tasks)")}
        predicates = ["id = ?", "status IN ('todo', 'ready')"]
        params: list[Any] = [task_id]
        for column in ("claim_lock", "claim_expires", "worker_pid", "current_run_id"):
            if column in columns:
                predicates.append(f"{column} IS NULL")
        assignments = ["status = 'blocked'"]
        if "block_kind" in columns:
            assignments.append("block_kind = ?")
            params.insert(0, "capability")
        cur = self._conn.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE {' AND '.join(predicates)}",
            tuple(params),
        )
        if cur.rowcount != 1:
            return False
        self._conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (
                task_id,
                "workspace_binding_quarantined",
                json.dumps({"reason": reason}, ensure_ascii=False),
                int(time.time()),
            ),
        )
        return True

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()


def _open_adapter(board: str | None, *, db_path: Path | None = None) -> _TaskAdapter:
    return _CoreTaskAdapter(board, db_path=db_path)


def _materialize_and_verify(raw: Mapping[str, Any], binding: RepoBinding) -> None:
    key = _exact_idempotency_key(raw["idempotency_key"])
    board = raw.get("board") if isinstance(raw.get("board"), str) else None
    with _creation_lock(board, key) as db_path:
        adapter = _open_adapter(board, db_path=db_path)
        try:
            verification_error: BindingError | None = None
            with adapter.transaction():
                existing = adapter.find_idempotent(key)
                task_id = existing or adapter.create(raw, binding)
                try:
                    _verify_readback(adapter, raw, binding, task_id)
                    if existing is None:
                        _verify_initial_status(adapter, raw, task_id)
                except BindingError as exc:
                    if existing is None:
                        raise
                    adapter.quarantine(task_id, str(exc))
                    verification_error = exc
            if verification_error is not None:
                raise verification_error
        finally:
            adapter.close()


def _option_values(args: list[str], option: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == option:
            if index + 1 >= len(args):
                raise BindingError(f"{option} is missing a value")
            values.append(args[index + 1])
            index += 2
            continue
        if value.startswith(option + "="):
            values.append(value.split("=", 1)[1])
        index += 1
    return values


def _parse_terminal_create_args(args: list[str]) -> argparse.Namespace:
    """Parse a terminal create tail with the authoritative Hermes CLI parser."""
    try:
        from hermes_cli import kanban_parser  # pyright: ignore[reportMissingImports]

        wrapper = argparse.ArgumentParser(add_help=False)
        wrapper.exit_on_error = False  # type: ignore[attr-defined]
        parser = kanban_parser.build_parser(wrapper.add_subparsers(dest="_top"))
        parser.exit_on_error = False  # type: ignore[attr-defined]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            parsed = parser.parse_args(["create", *args])
    except (argparse.ArgumentError, AttributeError, ImportError, SystemExit, TypeError, ValueError) as exc:
        raise BindingError(
            "terminal specialist create does not match Hermes CLI create syntax"
        ) from exc
    if getattr(parsed, "kanban_action", None) != "create":
        raise BindingError("terminal specialist command is not a Kanban create")
    return parsed


def _terminal_create_conversion(parsed: argparse.Namespace) -> dict[str, Any]:
    """Apply the same non-argparse conversions as ``hermes kanban create``."""
    try:
        from hermes_cli.kanban import (  # pyright: ignore[reportMissingImports]
            _parse_branch_flag,
            _parse_duration,
            _parse_workspace_flag,
        )

        workspace_kind, workspace_path = _parse_workspace_flag(parsed.workspace)
        branch_name = _parse_branch_flag(getattr(parsed, "branch", None))
        max_runtime = _parse_duration(getattr(parsed, "max_runtime", None))
    except (argparse.ArgumentTypeError, AttributeError, ImportError, TypeError, ValueError) as exc:
        raise BindingError("terminal specialist create value conversion failed") from exc

    max_retries = getattr(parsed, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        raise BindingError("--max-retries must be >= 1")
    if getattr(parsed, "provider_override", None) and not getattr(parsed, "model_override", None):
        raise BindingError("'--provider' requires '--model' to be set as well")

    raw: dict[str, Any] = {
        "title": parsed.title,
        "assignee": parsed.assignee,
        "created_by": parsed.created_by,
        "initial_status": parsed.initial_status,
    }
    for source, target in (
        ("body", "body"),
        ("project", "project"),
        ("tenant", "tenant"),
        ("idempotency_key", "idempotency_key"),
        ("completion_contract", "completion_contract"),
        ("model_override", "model"),
        ("provider_override", "provider"),
    ):
        value = getattr(parsed, source, None)
        if value is not None:
            raw[target] = value
    if parsed.priority:
        raw["priority"] = parsed.priority
    if parsed.parent:
        raw["parents"] = list(parsed.parent)
    if parsed.triage:
        raw["triage"] = True
    if workspace_kind is not None:
        raw["workspace_kind"] = workspace_kind
    if workspace_path is not None:
        raw["workspace_path"] = workspace_path
    if branch_name is not None:
        raw["branch_name"] = branch_name
    if max_runtime is not None:
        raw["max_runtime_seconds"] = max_runtime
    if parsed.skills:
        raw["skills"] = list(parsed.skills)
    if max_retries is not None:
        raw["max_retries"] = max_retries
    if parsed.goal_mode:
        raw["goal_mode"] = True
    if parsed.goal_max_turns is not None:
        raw["goal_max_turns"] = parsed.goal_max_turns
    return raw


def _terminal_create_input(args: list[str], board: str) -> dict[str, Any] | None:
    assignees = _option_values(args, "--assignee")
    _reject_unresolved_shell_substitutions(assignees)
    assignee = assignees[-1] if assignees else None
    specialist = _specialist(assignee)
    if specialist is None:
        return None
    substitution_values = [*args]
    if board:
        substitution_values.append(board)
    _reject_unresolved_shell_substitutions(substitution_values)
    raw = _terminal_create_conversion(_parse_terminal_create_args(args))
    raw["board"] = board or None
    return raw


def _specialist_invocations(command: str) -> list[tuple[str, list[str], str]]:
    """Reuse the existing lifecycle parser for terminal create wrappers."""
    path = Path(__file__).with_name("kanban-specialist-completion-guard.py")
    if not path.is_file():
        raise BindingError("terminal specialist parser dependency is missing")
    spec = importlib.util.spec_from_file_location("h4v3_specialist_parser", path)
    if spec is None or spec.loader is None:
        raise BindingError("terminal specialist parser cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    parser = getattr(module, "_hermes_kanban_invocations", None)
    if not callable(parser):
        raise BindingError("terminal specialist parser has no invocation reader")
    parser_fn = cast(Callable[[str], list[tuple[str, list[str], str]]], parser)
    try:
        return parser_fn(command)
    except RuntimeError as exc:
        raise BindingError(str(exc)) from exc


def _prepare_structured(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], RepoBinding] | None:
    raw = payload.get("tool_input")
    if not isinstance(raw, Mapping):
        raise BindingError("kanban_create tool_input must be an object")
    assignee = _specialist(raw.get("assignee"))
    if assignee is None:
        return None
    if _is_blocked_quarantine(raw):
        return None
    return raw, _resolve_binding(raw)


def _evaluate_structured(payload: Mapping[str, Any]) -> int:
    assignee = _specialist(
        payload.get("tool_input", {}).get("assignee")
        if isinstance(payload.get("tool_input"), Mapping)
        else None
    )
    try:
        prepared = _prepare_structured(payload)
        if prepared is not None:
            raw, binding = prepared
            _materialize_and_verify(raw, binding)
        return 0
    except (BindingError, OSError, sqlite3.Error) as exc:
        return _block(str(exc), assignee=assignee or "")


def _evaluate_terminal(payload: Mapping[str, Any]) -> int:
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, Mapping):
        return 0
    command = str(raw_input.get("command") or "")
    try:
        prepared: list[tuple[Mapping[str, Any], RepoBinding]] = []
        for action, args, board in _specialist_invocations(command):
            if action != "create":
                continue
            create_input = _terminal_create_input(args, board)
            if create_input is None:
                continue
            candidate = _prepare_structured(
                {"tool_name": "kanban_create", "tool_input": create_input}
            )
            if candidate is not None:
                prepared.append(candidate)
        for create_input, binding in prepared:
            _materialize_and_verify(create_input, binding)
        return 0
    except (BindingError, OSError, sqlite3.Error) as exc:
        return _block(str(exc), source="terminal")


def evaluate_payload(payload: Mapping[str, Any]) -> int:
    """Evaluate one already-decoded pre_tool_call payload."""
    tool_name = str(payload.get("tool_name") or "")
    if tool_name == "kanban_create":
        return _evaluate_structured(payload)
    if tool_name == "terminal":
        return _evaluate_terminal(payload)
    return 0


# Kept importable for focused tests and for the stable wrapper's in-process call.
__all__ = [
    "BindingError",
    "RepoBinding",
    "SPECIALIST_ASSIGNEES",
    "evaluate_payload",
    "_evaluate_structured",
    "_materialize_and_verify",
    "_open_adapter",
    "_resolve_binding",
]


if __name__ == "__main__":
    try:
        value = json.loads(sys.stdin.read() or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(_block(f"malformed pre_tool_call payload: {type(exc).__name__}"))
    raise SystemExit(evaluate_payload(value) if isinstance(value, Mapping) else 0)
