from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace


PLUGIN = Path(__file__).resolve().parents[1] / "hermes-plugin" / "kanban-protocol-loop-guard" / "__init__.py"


def _load():
    name = "kanban_protocol_loop_guard_test_subject"
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            worker_pid INTEGER,
            worker_started_at TEXT,
            claim_lock TEXT,
            current_run_id INTEGER
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            outcome TEXT,
            error TEXT,
            metadata TEXT,
            ended_at INTEGER
        );
        """
    )
    conn.execute("INSERT INTO tasks(id,status) VALUES('t_aaaaaaaa','ready')")
    conn.commit()
    return conn


def _run(conn: sqlite3.Connection, outcome: str, *, protocol: bool = False) -> None:
    conn.execute(
        "INSERT INTO task_runs(task_id,outcome,error,metadata,ended_at) VALUES(?,?,?,?,1)",
        (
            "t_aaaaaaaa",
            outcome,
            "worker exited cleanly: protocol violation" if protocol else "",
            json.dumps({"protocol_violation": True}) if protocol else "{}",
        ),
    )
    conn.commit()


def test_mixed_timeout_and_protocol_failures_share_five_attempt_streak(tmp_path):
    mod = _load()
    conn = _db(tmp_path / "kanban.db")
    _run(conn, "timed_out")
    _run(conn, "crashed", protocol=True)
    _run(conn, "timed_out")
    _run(conn, "crashed", protocol=True)
    assert mod._bounded_failure_streak(conn, "t_aaaaaaaa") == 4
    _run(conn, "timed_out")
    assert mod._bounded_failure_streak(conn, "t_aaaaaaaa") == 5
    conn.close()


def test_rate_limit_is_excluded_without_breaking_streak(tmp_path):
    mod = _load()
    conn = _db(tmp_path / "kanban.db")
    _run(conn, "timed_out")
    _run(conn, "rate_limited")
    _run(conn, "crashed", protocol=True)
    assert mod._bounded_failure_streak(conn, "t_aaaaaaaa") == 2
    conn.close()


def test_success_breaks_bounded_failure_streak(tmp_path):
    mod = _load()
    conn = _db(tmp_path / "kanban.db")
    _run(conn, "timed_out")
    _run(conn, "completed")
    _run(conn, "crashed", protocol=True)
    assert mod._bounded_failure_streak(conn, "t_aaaaaaaa") == 1
    conn.close()


def test_dispatch_tick_enforces_fifth_timeout(tmp_path, monkeypatch):
    mod = _load()
    conn = _db(tmp_path / "kanban.db")
    for _ in range(5):
        _run(conn, "timed_out")
    calls = []
    monkeypatch.setattr(mod, "_connect_board", lambda board: sqlite3.connect(tmp_path / "kanban.db"))
    monkeypatch.setattr(mod, "_enforce_if_needed", lambda db, task_id, board: calls.append((task_id, board)) or True)
    mod._on_dispatch_tick(board="demo", result=SimpleNamespace(timed_out=["t_aaaaaaaa"]))
    assert calls == [("t_aaaaaaaa", "demo")]
    conn.close()


def test_worker_exit_enforces_protocol_path(tmp_path, monkeypatch):
    mod = _load()
    calls = []
    monkeypatch.setattr(mod, "_enforce_task", lambda board, task_id: calls.append((board, task_id)))
    mod._on_worker_exited(
        board="demo",
        task_id="t_aaaaaaaa",
        exit_kind="clean_exit",
        outcome="crashed",
        retry_status="ready",
    )
    assert calls == [("demo", "t_aaaaaaaa")]


def test_dispatch_dry_run_is_inert(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_enforce_task", lambda *args: (_ for _ in ()).throw(AssertionError("mutated dry run")))
    mod._on_dispatch_tick(board="demo", dry_run=True, result=SimpleNamespace(timed_out=["t_aaaaaaaa"]))


def test_fifth_bounded_failure_sticky_blocks_ready_task(tmp_path, monkeypatch):
    mod = _load()
    conn = _db(tmp_path / "kanban.db")
    for index in range(5):
        _run(conn, "timed_out" if index % 2 == 0 else "crashed", protocol=index % 2 == 1)
    calls = []
    monkeypatch.setattr(
        mod,
        "_block",
        lambda db, task_id, expected_run_id=None: calls.append((task_id, expected_run_id)) or True,
    )
    assert mod._enforce_if_needed(conn, "t_aaaaaaaa", "demo") is True
    assert calls == [("t_aaaaaaaa", None)]
    conn.close()


def test_four_bounded_failures_do_not_block(tmp_path, monkeypatch):
    mod = _load()
    conn = _db(tmp_path / "kanban.db")
    for _ in range(4):
        _run(conn, "timed_out")
    monkeypatch.setattr(
        mod,
        "_block",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("blocked before fifth failure")),
    )
    assert mod._enforce_if_needed(conn, "t_aaaaaaaa", "demo") is False
    conn.close()
