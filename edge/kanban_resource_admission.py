#!/usr/bin/env python3
"""Opt-in cross-board worker resource admission for the edge rework lane.

Hermes' core dispatcher intentionally owns its normal scheduling semantics.
This module adds one edge-only capability for deployments where several
Kanban profiles share a constrained execution resource (for example, a local
inference endpoint that can serve only one long-context worker at a time).

The feature is deliberately provider/model agnostic.  Operators name arbitrary
resource groups and map assignee/profile globs to each group::

    kanban:
      worker_resources:
        local-inference:
          capacity: 1
          assignees:
            - "kanban-main"
            - "local-*"
          stale_worker_grace_seconds: 5

No ``worker_resources`` section (or no matching assignee) means no additional
admission gate at all: the original edge dispatcher is called directly, so
parallel-capable providers keep their existing behavior.

For a configured group the admission check is host-wide across sibling board
DBs.  A short file lock serializes the count + claim/spawn admission window,
while durable ``task_runs`` rows and live worker PIDs represent occupied slots.
Terminal runs whose Hermes worker process is still alive continue to occupy a
slot.  For the *same task* being reworked, such a superseded terminal worker is
verified by ``/proc/<pid>/cmdline`` and reaped before a new run may be claimed.
An actually active prior run is never killed; admission fails closed instead.
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


RESOURCE_CONFIG_KEY = "worker_resources"
DEFAULT_STALE_WORKER_GRACE_SECONDS = 5.0


class ResourceAdmissionError(RuntimeError):
    """Resource admission cannot make a safe scheduling decision."""


@dataclass(frozen=True)
class WorkerResource:
    name: str
    capacity: int
    assignees: tuple[str, ...]
    stale_worker_grace_seconds: float = DEFAULT_STALE_WORKER_GRACE_SECONDS

    def matches(self, assignee: str) -> bool:
        return any(fnmatch.fnmatchcase(assignee, pattern) for pattern in self.assignees)


def _resource_policies(cfg: Mapping[str, Any]) -> tuple[WorkerResource, ...]:
    raw = cfg.get(RESOURCE_CONFIG_KEY)
    if raw in (None, {}):
        return ()
    if not isinstance(raw, Mapping):
        raise ResourceAdmissionError(
            f"kanban.{RESOURCE_CONFIG_KEY} must be a mapping"
        )

    policies: list[WorkerResource] = []
    for raw_name, raw_spec in raw.items():
        name = str(raw_name or "").strip()
        if not name:
            raise ResourceAdmissionError("worker resource name must be non-empty")
        if not isinstance(raw_spec, Mapping):
            raise ResourceAdmissionError(
                f"worker resource {name!r} must be a mapping"
            )
        if raw_spec.get("enabled", True) is False:
            continue
        try:
            capacity = int(raw_spec.get("capacity"))
        except (TypeError, ValueError) as exc:
            raise ResourceAdmissionError(
                f"worker resource {name!r} capacity must be a positive integer"
            ) from exc
        if capacity < 1:
            raise ResourceAdmissionError(
                f"worker resource {name!r} capacity must be >= 1"
            )

        raw_assignees = raw_spec.get("assignees", ())
        if isinstance(raw_assignees, str):
            raw_assignees = (raw_assignees,)
        if not isinstance(raw_assignees, (list, tuple)):
            raise ResourceAdmissionError(
                f"worker resource {name!r} assignees must be a string or list"
            )
        assignees = tuple(
            str(item).strip() for item in raw_assignees if str(item).strip()
        )
        if not assignees:
            raise ResourceAdmissionError(
                f"worker resource {name!r} requires at least one assignee pattern"
            )

        try:
            grace = float(
                raw_spec.get(
                    "stale_worker_grace_seconds",
                    DEFAULT_STALE_WORKER_GRACE_SECONDS,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ResourceAdmissionError(
                f"worker resource {name!r} stale_worker_grace_seconds must be numeric"
            ) from exc
        if grace < 0 or grace > 60:
            raise ResourceAdmissionError(
                f"worker resource {name!r} stale_worker_grace_seconds must be between 0 and 60"
            )

        policies.append(
            WorkerResource(
                name=name,
                capacity=capacity,
                assignees=assignees,
                stale_worker_grace_seconds=grace,
            )
        )
    return tuple(policies)


def resource_for_assignee(
    cfg: Mapping[str, Any], assignee: Optional[str]
) -> Optional[WorkerResource]:
    """Resolve exactly one resource group for an assignee.

    Zero matches is the backwards-compatible path: no extra gate.  Multiple
    matches are rejected rather than selecting one by dict order because that
    would make scheduling depend on configuration ordering.
    """
    name = str(assignee or "").strip()
    if not name:
        return None
    matches = [policy for policy in _resource_policies(cfg) if policy.matches(name)]
    if not matches:
        return None
    if len(matches) > 1:
        raise ResourceAdmissionError(
            f"assignee {name!r} matches multiple worker resources: "
            + ", ".join(sorted(item.name for item in matches))
        )
    return matches[0]


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if sys.platform == "linux":
        try:
            status = Path(f"/proc/{value}/status").read_text(
                encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            return False
        except (PermissionError, OSError):
            return True
        for line in status.splitlines():
            if line.startswith("State:") and "Z" in line.split(":", 1)[1]:
                return False
    return True


def _worker_identity(pid: int, task_id: str) -> Optional[bool]:
    """Return True/False for a verified Linux cmdline, None if unverifiable.

    Hermes workers spawned by the Kanban dispatcher include the task id in
    the ``-q 'work kanban task <id>'`` command line.  We never terminate a
    terminal-run PID unless both that task id and a Hermes command marker are
    visible.  A False result therefore also safely handles OS PID reuse.
    """
    if sys.platform != "linux":
        return None
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except FileNotFoundError:
        return False
    except (PermissionError, OSError):
        return None
    command = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    folded = command.casefold()
    return task_id in command and "hermes" in folded


def _terminate_verified_worker(pid: int, grace_seconds: float) -> bool:
    """Terminate a verified detached Hermes worker process group."""
    target = int(pid)

    def send(sig: int) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(target, sig)
            else:
                os.kill(target, sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while _pid_alive(target) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_alive(target):
        send(signal.SIGKILL)
        deadline = time.monotonic() + 1.0
        while _pid_alive(target) and time.monotonic() < deadline:
            time.sleep(0.05)
    return not _pid_alive(target)


def _latest_task_run(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    try:
        return conn.execute(
            "SELECT id, task_id, profile, status, outcome, worker_pid, ended_at "
            "FROM task_runs WHERE task_id = ? AND worker_pid IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ResourceAdmissionError(
            f"could not inspect prior run for {task_id}: {type(exc).__name__}: {exc}"
        ) from exc


def _quiesce_superseded_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    grace_seconds: float,
) -> dict[str, Any]:
    """Reap one terminal-but-live prior worker for the same task.

    Active prior runs are never killed.  A PID that no longer belongs to this
    task (PID reuse) is ignored.  An alive PID whose identity cannot be
    verified fails closed.
    """
    run = _latest_task_run(conn, task_id)
    if run is None or run["worker_pid"] is None:
        return {"ok": True, "reaped": False}
    pid = int(run["worker_pid"])
    if not _pid_alive(pid):
        return {"ok": True, "reaped": False}
    if run["ended_at"] is None:
        return {
            "ok": False,
            "reason": "task_worker_active",
            "run_id": int(run["id"]),
            "pid": pid,
        }

    identity = _worker_identity(pid, task_id)
    if identity is False:
        # The recorded PID was reused by an unrelated process.  Never signal
        # it, and do not let the stale historical row consume capacity.
        return {
            "ok": True,
            "reaped": False,
            "pid_reused": True,
            "run_id": int(run["id"]),
            "pid": pid,
        }
    if identity is None:
        return {
            "ok": False,
            "reason": "superseded_worker_identity_unverified",
            "run_id": int(run["id"]),
            "pid": pid,
        }
    if not _terminate_verified_worker(pid, grace_seconds):
        return {
            "ok": False,
            "reason": "superseded_worker_reap_failed",
            "run_id": int(run["id"]),
            "pid": pid,
        }
    return {
        "ok": True,
        "reaped": True,
        "run_id": int(run["id"]),
        "pid": pid,
        "outcome": run["outcome"],
    }


def _board_db_paths(kanban_db: Any, board: str) -> tuple[Path, ...]:
    try:
        current = Path(kanban_db.kanban_db_path(board=board)).resolve()
    except Exception as exc:
        raise ResourceAdmissionError(
            f"could not resolve Kanban DB path: {type(exc).__name__}: {exc}"
        ) from exc

    # Standard board layout: <root>/kanban/boards/<board>/kanban.db.
    # Derive from the core-resolved current DB instead of hardcoding HERMES_HOME.
    board_dir = current.parent
    boards_root = board_dir.parent
    candidates = sorted(boards_root.glob("*/kanban.db"))
    if current not in candidates:
        candidates.append(current)
    return tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_file()))


def _assignee_matches(resource: WorkerResource, value: Any) -> bool:
    name = str(value or "").strip()
    return bool(name and resource.matches(name))


def _active_resource_workers(
    kanban_db: Any,
    board: str,
    resource: WorkerResource,
) -> list[dict[str, Any]]:
    """Return occupied resource slots across every sibling board DB.

    Live worker PIDs are sourced from durable run history, not task status,
    so a terminal run whose process has not exited still occupies capacity.
    A running task with no PID yet is counted as an in-flight reservation.
    """
    active: list[dict[str, Any]] = []
    seen_pids: set[int] = set()
    for path in _board_db_paths(kanban_db, board):
        try:
            db = sqlite3.connect(str(path), timeout=0.25)
            db.row_factory = sqlite3.Row
            try:
                run_rows = db.execute(
                    "SELECT r.id AS run_id, r.task_id, r.profile, r.worker_pid, "
                    "r.ended_at, t.assignee, t.status "
                    "FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id "
                    "WHERE r.worker_pid IS NOT NULL ORDER BY r.id DESC"
                ).fetchall()
                for row in run_rows:
                    assignee = row["profile"] or row["assignee"]
                    if not _assignee_matches(resource, assignee):
                        continue
                    pid = int(row["worker_pid"])
                    if pid in seen_pids or not _pid_alive(pid):
                        continue
                    identity = _worker_identity(pid, str(row["task_id"]))
                    if identity is False:
                        # Proven PID reuse: not this Hermes worker.
                        continue
                    seen_pids.add(pid)
                    active.append({
                        "board_db": str(path),
                        "task_id": str(row["task_id"]),
                        "run_id": int(row["run_id"]),
                        "assignee": str(assignee),
                        "pid": pid,
                        "terminal": row["ended_at"] is not None,
                        "identity_verified": identity is True,
                    })

                # Claim -> spawn has a small interval before worker_pid is
                # persisted. Count that as a reservation so a second edge
                # process cannot pass the capacity check in that window.
                task_rows = db.execute(
                    "SELECT id, assignee, worker_pid, current_run_id "
                    "FROM tasks WHERE status = 'running'"
                ).fetchall()
                for row in task_rows:
                    if not _assignee_matches(resource, row["assignee"]):
                        continue
                    if row["worker_pid"] is not None:
                        pid = int(row["worker_pid"])
                        if pid in seen_pids:
                            continue
                        if not _pid_alive(pid):
                            continue
                        seen_pids.add(pid)
                        active.append({
                            "board_db": str(path),
                            "task_id": str(row["id"]),
                            "run_id": int(row["current_run_id"])
                            if row["current_run_id"] is not None else None,
                            "assignee": str(row["assignee"]),
                            "pid": pid,
                            "terminal": False,
                            "identity_verified": _worker_identity(pid, str(row["id"])) is True,
                        })
                    else:
                        active.append({
                            "board_db": str(path),
                            "task_id": str(row["id"]),
                            "run_id": int(row["current_run_id"])
                            if row["current_run_id"] is not None else None,
                            "assignee": str(row["assignee"]),
                            "pid": None,
                            "terminal": False,
                            "identity_verified": False,
                            "reservation": True,
                        })
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise ResourceAdmissionError(
                f"could not inspect worker resource {resource.name!r} in {path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    return active


@contextlib.contextmanager
def _resource_lock(kanban_db: Any, board: str, resource_name: str):
    """Try to hold one host-local admission lock; yield True when acquired."""
    try:
        import fcntl  # POSIX; the deployed Hermes edge runs in Linux.
    except ImportError:
        yield False
        return

    db_paths = _board_db_paths(kanban_db, board)
    if not db_paths:
        yield False
        return
    boards_root = db_paths[0].parent.parent
    lock_dir = boards_root / ".resource-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(resource_name.encode("utf-8")).hexdigest()[:20]
    lock_path = lock_dir / f"{digest}.lock"
    with open(lock_path, "a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _entry(
    *,
    task_id: Optional[str],
    reason: str,
    resource: WorkerResource,
    board: str,
    **extra: Any,
) -> list[dict[str, Any]]:
    value: dict[str, Any] = {
        "task_id": task_id,
        "status": "ready" if task_id else None,
        "changed": False,
        "reason": reason,
        "board": board,
        "resource_group": resource.name,
        "resource_capacity": resource.capacity,
    }
    value.update(extra)
    return [value]


def install_resource_admission(edge_module: Any) -> None:
    """Wrap ``edge_module._dispatch_pending_rework`` exactly once.

    The wrapper is intentionally outside the large reconciliation module so
    the deployment can remain a small, reviewable edge overlay.  If no worker
    resource matches the pending task's assignee, it delegates immediately to
    the original function without taking a lock or scanning other boards.
    """
    original = getattr(edge_module, "_dispatch_pending_rework", None)
    if not callable(original):
        raise ResourceAdmissionError("edge module has no _dispatch_pending_rework")
    if getattr(original, "_resource_admission_installed", False):
        return

    def guarded_dispatch(
        conn: sqlite3.Connection,
        kanban_db: Any,
        board: str,
        *args: Any,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        cfg = kwargs.get("cfg")
        if cfg is None:
            cfg = edge_module._kanban_config()
        if not isinstance(cfg, Mapping):
            cfg = {}

        try:
            pending = edge_module._pending_rework_tasks(
                conn,
                task_ids=kwargs.get("task_ids"),
                normalized_task_ids=kwargs.get("normalized_task_ids"),
            )
        except Exception:
            # Preserve the original edge's own error/empty handling when the
            # admission overlay cannot even identify a candidate.
            return original(conn, kanban_db, board, *args, **kwargs)
        if not pending:
            return original(conn, kanban_db, board, *args, **kwargs)

        candidate = pending[0]
        task_id = str(candidate["id"])
        assignee = str(candidate["assignee"] or "").strip()
        if not assignee:
            assignee = str(cfg.get("default_assignee") or "").strip()

        try:
            resource = resource_for_assignee(cfg, assignee)
        except ResourceAdmissionError as exc:
            return [{
                "task_id": task_id,
                "status": "ready",
                "changed": False,
                "reason": "resource_config_invalid",
                "board": board,
                "error": str(exc),
            }]
        if resource is None:
            # Critical backwards-compatibility path: no matching resource
            # means exactly the existing edge dispatch semantics.
            return original(conn, kanban_db, board, *args, **kwargs)

        try:
            with _resource_lock(kanban_db, board, resource.name) as held:
                if not held:
                    return _entry(
                        task_id=task_id,
                        reason="resource_locked",
                        resource=resource,
                        board=board,
                    )

                reap: dict[str, Any] = {"ok": True, "reaped": False}
                if not bool(kwargs.get("dry_run")):
                    reap = _quiesce_superseded_worker(
                        conn,
                        task_id,
                        grace_seconds=resource.stale_worker_grace_seconds,
                    )
                    if not reap.get("ok"):
                        return _entry(
                            task_id=task_id,
                            reason=str(reap.get("reason") or "resource_admission_failed"),
                            resource=resource,
                            board=board,
                            run_id=reap.get("run_id"),
                            pid=reap.get("pid"),
                        )

                active = _active_resource_workers(kanban_db, board, resource)
                if len(active) >= resource.capacity:
                    return _entry(
                        task_id=task_id,
                        reason="resource_busy",
                        resource=resource,
                        board=board,
                        resource_active=len(active),
                        active_workers=active,
                    )

                result = original(conn, kanban_db, board, *args, **kwargs)
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    item.setdefault("resource_group", resource.name)
                    item.setdefault("resource_capacity", resource.capacity)
                    if reap.get("reaped"):
                        item.setdefault("superseded_run_reaped", {
                            "run_id": reap.get("run_id"),
                            "pid": reap.get("pid"),
                            "outcome": reap.get("outcome"),
                        })
                return result
        except (ResourceAdmissionError, OSError) as exc:
            return _entry(
                task_id=task_id,
                reason="resource_admission_failed",
                resource=resource,
                board=board,
                error=f"{type(exc).__name__}: {exc}",
            )

    guarded_dispatch._resource_admission_installed = True  # type: ignore[attr-defined]
    guarded_dispatch._resource_admission_original = original  # type: ignore[attr-defined]
    edge_module._dispatch_pending_rework = guarded_dispatch
