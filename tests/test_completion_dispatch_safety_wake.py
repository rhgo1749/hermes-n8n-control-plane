from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "hermes-plugin"
    / "github-completion-dispatch-safety-wake"
    / "__init__.py"
)


def _load():
    name = "completion_dispatch_safety_test_subject"
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod._ATTEMPTS.clear()
    mod._PRIMARY_MODULE = None
    return mod


def _db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            body TEXT
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    con.commit()
    con.close()


def _insert(
    db: Path,
    *,
    task_id: str,
    status: str,
    body: str,
    event_id: int,
    created_at: int,
) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO tasks(id,status,body) VALUES(?,?,?)",
        (task_id, status, body),
    )
    con.execute(
        """
        INSERT INTO task_events(id,task_id,kind,created_at)
        VALUES(?,?,'completed',?)
        """,
        (event_id, task_id, created_at),
    )
    con.commit()
    con.close()


GITHUB_BODY = """# Imported GitHub Issue
- source: github-issue
- completion contract: github-pr

## Canonical Issue body
hello
"""


class FakePrimary:
    def __init__(self, db: Path, *, project: bool = True) -> None:
        self.db = db
        self.calls: list[tuple[str, str]] = []
        self.project = project

    def _completion_is_eligible(self, task_id: str, board: str) -> bool:
        con = sqlite3.connect(self.db)
        row = con.execute(
            "SELECT status, body FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        con.close()
        return bool(
            row
            and row[0] == "done"
            and ("completion contract: github-pr" in (row[1] or ""))
        )

    def _on_task_completed(self, *, task_id: str, board: str) -> None:
        self.calls.append((task_id, board))
        if self.project:
            con = sqlite3.connect(self.db)
            con.execute(
                "UPDATE tasks SET status='review' WHERE id = ?",
                (task_id,),
            )
            con.commit()
            con.close()


def test_stranded_recent_github_completion_replays_primary(tmp_path, monkeypatch):
    mod = _load()
    db = tmp_path / "kanban.db"
    _db(db)
    _insert(
        db,
        task_id="t_aaaaaaaa",
        status="done",
        body=GITHUB_BODY,
        event_id=7,
        created_at=1000,
    )
    primary = FakePrimary(db)

    monkeypatch.setattr(mod, "_board_db_path", lambda board: db)
    monkeypatch.setattr(mod, "_load_primary", lambda: primary)
    monkeypatch.setattr(mod.time, "time", lambda: 1005)

    mod._on_dispatch_tick(board="demo")

    assert primary.calls == [("t_aaaaaaaa", "demo")]
    con = sqlite3.connect(db)
    assert con.execute("SELECT status FROM tasks").fetchone()[0] == "review"
    con.close()


def test_non_github_done_is_ignored(tmp_path, monkeypatch):
    mod = _load()
    db = tmp_path / "kanban.db"
    _db(db)
    _insert(
        db,
        task_id="t_bbbbbbbb",
        status="done",
        body="ordinary task",
        event_id=8,
        created_at=1000,
    )
    primary = FakePrimary(db)

    monkeypatch.setattr(mod, "_board_db_path", lambda board: db)
    monkeypatch.setattr(mod, "_load_primary", lambda: primary)
    monkeypatch.setattr(mod.time, "time", lambda: 1005)

    mod._on_dispatch_tick(board="demo")
    assert primary.calls == []


def test_already_projected_review_is_ignored(tmp_path, monkeypatch):
    mod = _load()
    db = tmp_path / "kanban.db"
    _db(db)
    _insert(
        db,
        task_id="t_cccccccc",
        status="review",
        body=GITHUB_BODY,
        event_id=9,
        created_at=1000,
    )
    primary = FakePrimary(db)

    monkeypatch.setattr(mod, "_board_db_path", lambda board: db)
    monkeypatch.setattr(mod, "_load_primary", lambda: primary)
    monkeypatch.setattr(mod.time, "time", lambda: 1005)

    mod._on_dispatch_tick(board="demo")
    assert primary.calls == []


def test_old_completion_outside_lookback_is_ignored(tmp_path, monkeypatch):
    mod = _load()
    db = tmp_path / "kanban.db"
    _db(db)
    _insert(
        db,
        task_id="t_dddddddd",
        status="done",
        body=GITHUB_BODY,
        event_id=10,
        created_at=1,
    )
    primary = FakePrimary(db)

    monkeypatch.setattr(mod, "_board_db_path", lambda board: db)
    monkeypatch.setattr(mod, "_load_primary", lambda: primary)
    monkeypatch.setattr(mod.time, "time", lambda: 5000)

    mod._on_dispatch_tick(board="demo")
    assert primary.calls == []


def test_still_done_replay_is_bounded_to_two_attempts(tmp_path, monkeypatch):
    mod = _load()
    db = tmp_path / "kanban.db"
    _db(db)
    _insert(
        db,
        task_id="t_eeeeeeee",
        status="done",
        body=GITHUB_BODY,
        event_id=11,
        created_at=1000,
    )
    primary = FakePrimary(db, project=False)

    monkeypatch.setattr(mod, "_board_db_path", lambda board: db)
    monkeypatch.setattr(mod, "_load_primary", lambda: primary)
    monkeypatch.setattr(mod.time, "time", lambda: 1005)

    mod._on_dispatch_tick(board="demo")
    mod._on_dispatch_tick(board="demo")
    mod._on_dispatch_tick(board="demo")
    mod._on_dispatch_tick(board="demo")

    assert primary.calls == [
        ("t_eeeeeeee", "demo"),
        ("t_eeeeeeee", "demo"),
    ]


def test_one_edge_replay_can_clear_multiple_stranded_tasks(tmp_path, monkeypatch):
    mod = _load()
    db = tmp_path / "kanban.db"
    _db(db)
    _insert(
        db,
        task_id="t_11111111",
        status="done",
        body=GITHUB_BODY,
        event_id=21,
        created_at=1000,
    )
    _insert(
        db,
        task_id="t_22222222",
        status="done",
        body=GITHUB_BODY,
        event_id=22,
        created_at=1001,
    )

    class BoardPrimary(FakePrimary):
        def _on_task_completed(self, *, task_id: str, board: str) -> None:
            self.calls.append((task_id, board))
            con = sqlite3.connect(self.db)
            con.execute("UPDATE tasks SET status='review' WHERE status='done'")
            con.commit()
            con.close()

    primary = BoardPrimary(db)
    monkeypatch.setattr(mod, "_board_db_path", lambda board: db)
    monkeypatch.setattr(mod, "_load_primary", lambda: primary)
    monkeypatch.setattr(mod.time, "time", lambda: 1005)

    mod._on_dispatch_tick(board="demo")
    mod._on_dispatch_tick(board="demo")

    assert primary.calls == [("t_11111111", "demo")]


def test_dispatch_dry_run_never_replays_or_reads_board(tmp_path, monkeypatch):
    mod = _load()

    monkeypatch.setattr(
        mod,
        "_recent_stranded_completions",
        lambda board: (_ for _ in ()).throw(AssertionError("board read during dry-run")),
    )
    monkeypatch.setattr(
        mod,
        "_load_primary",
        lambda: (_ for _ in ()).throw(AssertionError("primary load during dry-run")),
    )

    mod._on_dispatch_tick(board="demo", dry_run=True)
