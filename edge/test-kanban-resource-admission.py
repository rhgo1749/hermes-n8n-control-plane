#!/usr/bin/env python3
"""Isolated tests for configurable edge worker-resource admission.

No Hermes install or real process is required.  Temporary sibling board DBs
exercise cross-board capacity counting while PID/process identity helpers are
patched deterministically.
"""
from __future__ import annotations

import sqlite3
import tempfile
import types
from pathlib import Path

import kanban_resource_admission as admission


PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            assignee TEXT,
            status TEXT,
            worker_pid INTEGER,
            current_run_id INTEGER
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT,
            profile TEXT,
            status TEXT,
            outcome TEXT,
            worker_pid INTEGER,
            ended_at INTEGER
        );
        """
    )
    conn.commit()
    return conn


class FakeKanban:
    def __init__(self, root: Path):
        self.root = root

    def kanban_db_path(self, *, board: str):
        return self.root / "boards" / board / "kanban.db"


def edge_module(calls: list[str], pending_assignee: str = "local-worker"):
    edge = types.ModuleType("fake_edge")

    def original(conn, kanban_db, board, *args, **kwargs):
        calls.append(board)
        return [{
            "task_id": "t-new",
            "status": "running",
            "changed": True,
            "reason": "rework_worker_spawned",
        }]

    edge._dispatch_pending_rework = original
    edge._kanban_config = lambda: {}
    edge._pending_rework_tasks = lambda conn, **kwargs: [{
        "id": "t-new",
        "assignee": pending_assignee,
    }]
    admission.install_resource_admission(edge)
    return edge


def cfg(capacity=1, assignees=("local-*",)):
    return {
        "worker_resources": {
            "local-inference": {
                "capacity": capacity,
                "assignees": list(assignees),
                "stale_worker_grace_seconds": 0,
            }
        }
    }


def test_unconfigured_and_unmatched_delegate() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-admission-none-"))
    kdb = FakeKanban(root)
    conn = make_db(kdb.kanban_db_path(board="a"))
    try:
        calls: list[str] = []
        edge = edge_module(calls)
        result = edge._dispatch_pending_rework(conn, kdb, "a", cfg={})
        check("unconfigured delegates", result[0]["reason"] == "rework_worker_spawned")
        check("unconfigured calls original", calls == ["a"], str(calls))

        calls.clear()
        result = edge._dispatch_pending_rework(
            conn,
            kdb,
            "a",
            cfg=cfg(1, assignees=("cloud-*",)),
        )
        check("unmatched parallel profile delegates", result[0]["reason"] == "rework_worker_spawned")
        check("unmatched calls original", calls == ["a"], str(calls))
    finally:
        conn.close()


def test_cross_board_capacity() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-admission-cap-"))
    kdb = FakeKanban(root)
    conn_a = make_db(kdb.kanban_db_path(board="a"))
    conn_b = make_db(kdb.kanban_db_path(board="b"))
    conn_b.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-live", "local-worker", "running", 9001, 1),
    )
    conn_b.execute(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (1, "t-live", "local-worker", "running", None, 9001, None),
    )
    conn_b.commit()

    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    admission._pid_alive = lambda pid: int(pid) == 9001
    admission._worker_identity = lambda pid, task_id: True
    try:
        calls: list[str] = []
        edge = edge_module(calls)
        result = edge._dispatch_pending_rework(conn_a, kdb, "a", cfg=cfg(1))
        check("capacity one blocks sibling board", result[0]["reason"] == "resource_busy", str(result))
        check("capacity one does not call original", calls == [], str(calls))
        check("busy evidence names group", result[0].get("resource_group") == "local-inference")
        check("busy evidence counts one", result[0].get("resource_active") == 1, str(result))

        calls.clear()
        result = edge._dispatch_pending_rework(conn_a, kdb, "a", cfg=cfg(2))
        check("capacity two admits second worker", result[0]["reason"] == "rework_worker_spawned", str(result))
        check("capacity two calls original", calls == ["a"], str(calls))
        check("spawn result carries group", result[0].get("resource_group") == "local-inference")
    finally:
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        conn_a.close()
        conn_b.close()


def test_terminal_same_task_worker_is_reaped() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-admission-reap-"))
    kdb = FakeKanban(root)
    conn = make_db(kdb.kanban_db_path(board="a"))
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-new", "local-worker", "ready", None, None),
    )
    conn.execute(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (201, "t-new", "local-worker", "review_requested", "review_requested", 9002, 123),
    )
    conn.commit()

    live = {9002}
    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    old_terminate = admission._terminate_verified_worker
    admission._pid_alive = lambda pid: int(pid) in live
    admission._worker_identity = lambda pid, task_id: int(pid) == 9002 and task_id == "t-new"

    def terminate(pid, grace):
        live.discard(int(pid))
        return True

    admission._terminate_verified_worker = terminate
    try:
        calls: list[str] = []
        edge = edge_module(calls)
        result = edge._dispatch_pending_rework(conn, kdb, "a", cfg=cfg(1))
        check("terminal stale worker is reaped", calls == ["a"], str(result))
        check(
            "reaped run evidence",
            result[0].get("superseded_run_reaped", {}).get("run_id") == 201,
            str(result),
        )
        check(
            "reaped pid evidence",
            result[0].get("superseded_run_reaped", {}).get("pid") == 9002,
            str(result),
        )
    finally:
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        admission._terminate_verified_worker = old_terminate
        conn.close()


def test_active_same_task_worker_is_never_killed() -> None:
    root = Path(tempfile.mkdtemp(prefix="resource-admission-active-"))
    kdb = FakeKanban(root)
    conn = make_db(kdb.kanban_db_path(board="a"))
    conn.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        ("t-new", "local-worker", "ready", None, 201),
    )
    conn.execute(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (201, "t-new", "local-worker", "running", None, 9003, None),
    )
    conn.commit()

    old_alive = admission._pid_alive
    old_identity = admission._worker_identity
    old_terminate = admission._terminate_verified_worker
    killed: list[int] = []
    admission._pid_alive = lambda pid: int(pid) == 9003
    admission._worker_identity = lambda pid, task_id: True
    admission._terminate_verified_worker = lambda pid, grace: killed.append(int(pid)) or True
    try:
        calls: list[str] = []
        edge = edge_module(calls)
        result = edge._dispatch_pending_rework(conn, kdb, "a", cfg=cfg(1))
        check("active prior run blocks new spawn", result[0]["reason"] == "task_worker_active", str(result))
        check("active prior run not killed", killed == [], str(killed))
        check("active prior run does not delegate", calls == [], str(calls))
    finally:
        admission._pid_alive = old_alive
        admission._worker_identity = old_identity
        admission._terminate_verified_worker = old_terminate
        conn.close()


def test_ambiguous_assignment_fails_closed() -> None:
    config = {
        "worker_resources": {
            "one": {"capacity": 1, "assignees": ["local-*"]},
            "two": {"capacity": 2, "assignees": ["*-worker"]},
        }
    }
    try:
        admission.resource_for_assignee(config, "local-worker")
    except admission.ResourceAdmissionError:
        check("ambiguous resource mapping rejected", True)
    else:
        check("ambiguous resource mapping rejected", False)


def test_stray_archive_db_is_not_enumerated() -> None:
    """Regression: a stray zero-byte ``kanban.db`` directly inside the archive
    container (``boards/_archived/kanban.db``) must not be enumerated by the
    base cross-board scan, nor poison ``_active_resource_workers``.
    """
    root = Path(tempfile.mkdtemp(prefix="resource-admission-stray-"))
    kdb = FakeKanban(root)
    conn_a = make_db(kdb.kanban_db_path(board="a"))
    stray = kdb.kanban_db_path(board="_archived")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"")
    try:
        paths = set(admission._board_db_paths(kdb, "a"))
        check(
            "stray _archived/kanban.db is not enumerated",
            stray.resolve() not in paths,
            str(paths),
        )
        check(
            "current board db remains enumerated",
            kdb.kanban_db_path(board="a").resolve() in paths,
            str(paths),
        )
        resource = admission.WorkerResource("serial", 1, ("local-worker",))
        try:
            active = admission._active_resource_workers(kdb, "a", resource)
        except admission.ResourceAdmissionError as exc:
            active = f"RAISED {type(exc).__name__}: {exc}"
        check(
            "worker inspection resolves despite stray file",
            isinstance(active, list),
            str(active),
        )
    finally:
        conn_a.close()


def main() -> int:
    test_unconfigured_and_unmatched_delegate()
    test_cross_board_capacity()
    test_terminal_same_task_worker_is_reaped()
    test_active_same_task_worker_is_never_killed()
    test_ambiguous_assignment_fails_closed()
    test_stray_archive_db_is_not_enumerated()
    print(f"\n{len(PASS)} passed; {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
