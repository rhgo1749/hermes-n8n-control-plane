#!/usr/bin/env python3
"""Deterministic creation-time workspace graph preflight regressions."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "automation/hermes/scripts/kanban-workspace-binding-guard.py"
STABLE_GUARD = ROOT / "automation/hermes/scripts/kanban-block-kind-guard.py"
HERMES_AGENT_ROOT = Path(os.environ.get("HERMES_AGENT_SOURCE_ROOT", "/ws/hermes-agent"))
HERMES_PYTHON = HERMES_AGENT_ROOT / "venv/bin/python3"


def _load_guard() -> Any:
    if str(HERMES_AGENT_ROOT) not in sys.path:
        sys.path.insert(0, str(HERMES_AGENT_ROOT))
    spec = importlib.util.spec_from_file_location("workspace_binding_guard_test", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeAdapter:
    def __init__(self, guard: Any, db_path: Path, *, corrupt_field: str | None = None) -> None:
        self.guard = guard
        self.corrupt_field = corrupt_field
        self.conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def find_idempotent(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT id, created_at FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC",
            (key,),
        ).fetchall()
        if not row:
            return None
        newest = int(row[0]["created_at"])
        if sum(int(candidate["created_at"]) == newest for candidate in row) > 1:
            raise self.guard.BindingError("same-key legacy rows have tied creation timestamps")
        return str(row[0]["id"])

    def create(self, raw: dict[str, Any], binding: Any) -> str:
        existing = self.find_idempotent(str(raw["idempotency_key"]))
        if existing:
            return existing
        row = self.conn.execute("SELECT COUNT(*) AS count FROM tasks").fetchone()
        task_id = f"t_{int(row['count']) + 1:08x}"
        parents = self.guard._parents(raw)
        statuses = self.parent_statuses(parents)
        status = self.guard._expected_status(raw, statuses)
        title = str(raw["title"]).strip()
        workspace_kind = "scratch" if self.corrupt_field == "workspace_kind" else "worktree"
        workspace_path = (
            str(binding.anchor)
            if self.corrupt_field == "workspace_path"
            else str(binding.anchor / ".worktrees" / task_id)
        )
        branch_name = (
            "shared-branch"
            if self.corrupt_field == "branch_name"
            else self.guard._branch_name(binding, task_id, title)
        )
        project_id = "wrong-project" if self.corrupt_field == "project_id" else binding.project_id
        idempotency_key = (
            "other-round"
            if self.corrupt_field == "idempotency_key"
            else str(raw["idempotency_key"])
        )
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, workspace_kind, workspace_path, "
            "branch_name, project_id, idempotency_key, created_at, created_by, "
            "max_runtime_seconds, skills, max_retries, goal_mode, goal_max_turns) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title,
                raw["assignee"],
                status,
                workspace_kind,
                workspace_path,
                branch_name,
                project_id,
                idempotency_key,
                1,
                raw.get("created_by") or "worker",
                self.guard._as_optional_int(
                    raw.get("max_runtime_seconds"), field="max_runtime_seconds"
                ),
                json.dumps(raw["skills"]) if raw.get("skills") is not None else None,
                self.guard._as_optional_int(raw.get("max_retries"), field="max_retries"),
                1 if self.guard._as_bool(raw.get("goal_mode"), field="goal_mode") else 0,
                self.guard._as_optional_int(raw.get("goal_max_turns"), field="goal_max_turns"),
            ),
        )
        for parent in parents:
            self.conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent, task_id),
            )
        return task_id

    def read(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def links(self, task_id: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT parent_id, child_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        return [(str(row["parent_id"]), str(row["child_id"])) for row in rows]

    def parent_statuses(self, parent_ids: tuple[str, ...]) -> dict[str, str]:
        if not parent_ids:
            return {}
        placeholders = ",".join("?" * len(parent_ids))
        rows = self.conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({placeholders})", parent_ids
        ).fetchall()
        return {str(row["id"]): str(row["status"]) for row in rows}

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def quarantine(self, task_id: str, reason: str) -> bool:
        cur = self.conn.execute(
            "UPDATE tasks SET status = 'blocked' "
            "WHERE id = ? AND status IN ('todo', 'ready') "
            "AND claim_lock IS NULL AND claim_expires IS NULL "
            "AND worker_pid IS NULL AND current_run_id IS NULL",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        self.conn.execute(
            "INSERT INTO task_events (task_id, kind, payload) VALUES (?, ?, ?)",
            (task_id, "workspace_binding_quarantined", json.dumps({"reason": reason})),
        )
        return True

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    guard = _load_guard()
    home = tmp_path / "hermes"
    home.mkdir()
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    projects = home / "projects.db"
    conn = sqlite3.connect(projects)
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT, primary_path TEXT, archived INTEGER)"
    )
    conn.execute(
        "INSERT INTO projects (id, slug, primary_path, archived) VALUES (?, ?, ?, 0)",
        ("p_control", "control-plane", str(repo)),
    )
    conn.commit()
    conn.close()
    board = tmp_path / "kanban.db"
    conn = sqlite3.connect(board)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, "
        "workspace_kind TEXT, workspace_path TEXT, branch_name TEXT, project_id TEXT, "
        "idempotency_key TEXT, created_at INTEGER NOT NULL DEFAULT 0, created_by TEXT, "
        "max_runtime_seconds INTEGER, skills TEXT, max_retries INTEGER, goal_mode INTEGER "
        "NOT NULL DEFAULT 0, goal_max_turns INTEGER, claim_lock TEXT, claim_expires INTEGER, "
        "worker_pid INTEGER, current_run_id INTEGER, block_kind TEXT)"
    )
    conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    conn.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT, payload TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))
    monkeypatch.setenv("KANBAN_WORKSPACE_BINDING_GUARD_LOG", str(tmp_path / "guard.log"))
    adapters: list[FakeAdapter] = []

    def open_adapter(_: str | None, *, db_path: Path | None = None) -> FakeAdapter:
        del db_path
        adapter = FakeAdapter(guard, board)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(guard, "_open_adapter", open_adapter)
    monkeypatch.setattr(guard, "_board_db_path", lambda _: board)
    return {"guard": guard, "repo": repo, "board": board, "adapters": adapters}


def _valid(repo: Path, *, key: str = "github:owner/repo:issue:138:round:1") -> dict[str, Any]:
    del repo
    return {
        "title": "Issue #138 developer round 1",
        "assignee": "kanban-developer",
        "body": "bounded implementation",
        "workspace_kind": "worktree",
        "project": "control-plane",
        "idempotency_key": key,
    }


def _count_tasks(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    finally:
        conn.close()


_REAL_HANDLER_REGRESSION = r'''
from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

repo_root = Path(sys.argv[1])
scenario = sys.argv[2]
hermes_root = Path(os.environ.get("HERMES_AGENT_SOURCE_ROOT", "/ws/hermes-agent"))
sys.path.insert(0, str(hermes_root))
for name in (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_TASK",
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_KEY",
):
    os.environ.pop(name, None)

with tempfile.TemporaryDirectory(prefix="issue138-real-regression-") as temp:
    root = Path(temp)
    home = root / "hermes"
    repo = root / "repo"
    board = root / "kanban.db"
    home.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    os.environ.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_KANBAN_DB": str(board),
            "HERMES_PROFILE": "test-worker",
            "KANBAN_WORKSPACE_BINDING_GUARD_LOG": str(root / "guard.log"),
        }
    )

    stable_guard = repo_root / "automation/hermes/scripts/kanban-block-kind-guard.py"

    def preflight(payload: dict[str, object]) -> int:
        result = subprocess.run(
            [sys.executable, str(stable_guard)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            check=False,
        )
        assert result.returncode in {0, 2}, result.stderr or result.stdout
        return result.returncode

    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    kb._INITIALIZED_PATHS.clear()
    pdb._INITIALIZED_PATHS.clear()
    with pdb.connect_closing() as conn:
        pdb.create_project(
            conn,
            name="Control Plane",
            slug="control-plane",
            primary_path=str(repo),
        )
    kb.init_db()

    def rows() -> list[tuple[str, str, str]]:
        with sqlite3.connect(board) as conn:
            return [
                tuple(row)
                for row in conn.execute(
                    "SELECT id, idempotency_key, status FROM tasks ORDER BY id"
                ).fetchall()
            ]

    def handler(key: str, **fields: object) -> dict[str, object]:
        from tools import kanban_tools as kt

        payload = {
            "title": "Issue #138 real handler",
            "assignee": "kanban-developer",
            "workspace_kind": "worktree",
            "project": "control-plane",
            "idempotency_key": key,
            **fields,
        }
        return json.loads(kt._handle_create(payload))

    if scenario == "padded":
        padded = " github:owner/repo:issue:138:real-padded "
        exact = padded.strip()
        padded_input = {
            "title": "Issue #138 real handler",
            "assignee": "kanban-developer",
            "workspace_kind": "worktree",
            "project": "control-plane",
            "idempotency_key": padded,
        }
        exact_input = {**padded_input, "idempotency_key": exact}
        padded_rc = preflight(
            {"tool_name": "kanban_create", "tool_input": padded_input}
        )
        after_padded = rows()
        first_guard_rc = preflight(
            {"tool_name": "kanban_create", "tool_input": exact_input}
        )
        replay_guard_rc = preflight(
            {"tool_name": "kanban_create", "tool_input": exact_input}
        )
        first_handler = handler(exact)
        replay_handler = handler(exact)
        print(
            json.dumps(
                {
                    "padded_rc": padded_rc,
                    "after_padded": after_padded,
                    "first_guard_rc": first_guard_rc,
                    "replay_guard_rc": replay_guard_rc,
                    "first_handler": first_handler,
                    "replay_handler": replay_handler,
                    "rows": rows(),
                }
            )
        )
    elif scenario == "cli-surface":
        command = (
            "hermes kanban create 'Issue #138 cli surface real' --assignee kanban-developer "
            "--workspace worktree --project control-plane "
            "--idempotency-key github:owner/repo:issue:138:real-cli-surface "
            "--skill translation --skill github-code-review --max-retries 3 "
            "--created-by cli-author --goal --goal-max-turns 7 --max-runtime 30m --json"
        )
        shell_rc = preflight({"tool_name": "terminal", "tool_input": {"command": command}})
        from hermes_cli.kanban_parser import build_parser

        wrapper = argparse.ArgumentParser(add_help=False)
        wrapper.exit_on_error = False
        parser = build_parser(wrapper.add_subparsers(dest="_top"))
        parser.exit_on_error = False
        cli_args = parser.parse_args(shlex.split(command)[2:])
        from hermes_cli.kanban import _cmd_create

        cli_rc = _cmd_create(cli_args)
        with sqlite3.connect(board) as conn:
            row = conn.execute(
                "SELECT created_by, max_runtime_seconds, skills, max_retries, goal_mode, "
                "goal_max_turns FROM tasks WHERE idempotency_key = ?",
                ("github:owner/repo:issue:138:real-cli-surface",),
            ).fetchone()
        structured_key = "github:owner/repo:issue:138:real-structured-surface"
        structured_fields = {
            "skills": ["translation", "github-code-review"],
            "max_runtime_seconds": 1800,
            "goal_mode": True,
            "goal_max_turns": 7,
        }
        structured_guard_rc = preflight(
            {
                "tool_name": "kanban_create",
                "tool_input": {
                    "title": "Issue #138 real handler",
                    "assignee": "kanban-developer",
                    "workspace_kind": "worktree",
                    "project": "control-plane",
                    "idempotency_key": structured_key,
                    **structured_fields,
                },
            }
        )
        structured_handler = handler(structured_key, **structured_fields)
        with sqlite3.connect(board) as conn:
            structured_row = conn.execute(
                "SELECT max_runtime_seconds, skills, goal_mode, goal_max_turns "
                "FROM tasks WHERE idempotency_key = ?",
                (structured_key,),
            ).fetchone()
        print(
            json.dumps(
                {
                    "shell_rc": shell_rc,
                    "cli_rc": cli_rc,
                    "row": row,
                    "structured_guard_rc": structured_guard_rc,
                    "structured_handler": structured_handler,
                    "structured_row": structured_row,
                }
            )
        )
    else:
        expanded = "github:owner/repo:issue:138:real-shell"
        command = (
            "export KEY="
            + expanded
            + "; sh -c 'hermes kanban create Issue #138 real handler "
            "--assignee kanban-developer --workspace worktree "
            "--project control-plane --idempotency-key \"$KEY\"'"
        )
        shell_rc = preflight(
            {"tool_name": "terminal", "tool_input": {"command": command}}
        )
        handler_result = handler(expanded)
        print(
            json.dumps(
                {
                    "shell_rc": shell_rc,
                    "handler": handler_result,
                    "rows": rows(),
                }
            )
        )
'''


def _run_real_handler_regression(scenario: str) -> dict[str, Any]:
    assert HERMES_PYTHON.is_file(), f"Hermes test runtime is missing: {HERMES_PYTHON}"
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    result = subprocess.run(
        [str(HERMES_PYTHON), "-c", _REAL_HANDLER_REGRESSION, str(ROOT), scenario],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


_REAL_ATOMIC_REGRESSION = r"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

repo_root = Path(sys.argv[1])
scenario = sys.argv[2]
hermes_root = Path(os.environ.get("HERMES_AGENT_SOURCE_ROOT", "/ws/hermes-agent"))
sys.path.insert(0, str(hermes_root))
for name in (
    "HERMES_DELEGATED_CHILD_CONTEXT",
    "HERMES_KANBAN_TASK",
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_KEY",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_BOARD",
):
    os.environ.pop(name, None)


def load_guard():
    path = repo_root / "automation/hermes/scripts/kanban-workspace-binding-guard.py"
    spec = importlib.util.spec_from_file_location("real_atomic_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def setup_environment(root: Path):
    home = root / "profile"
    shared = root / "shared-kanban"
    repo = root / "repo"
    home.mkdir()
    repo.mkdir()
    named = shared / "kanban" / "boards" / "ctrl-hangul"
    named.mkdir(parents=True)
    (named / "board.json").write_text("{}", encoding="utf-8")
    (shared / "kanban" / "current").write_text("ctrl-hangul\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    os.environ.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_KANBAN_HOME": str(shared),
            "HERMES_PROFILE": "test-worker",
            "KANBAN_WORKSPACE_BINDING_GUARD_LOG": str(root / "guard.log"),
        }
    )
    from hermes_cli import kanban_db as kb
    from hermes_cli import projects_db as pdb

    kb._INITIALIZED_PATHS.clear()
    pdb._INITIALIZED_PATHS.clear()
    with pdb.connect_closing() as conn:
        pdb.create_project(
            conn,
            name="Control Plane",
            slug="control-plane",
            primary_path=str(repo),
        )
    kb.init_db(board="ctrl-hangul")
    return home, shared, repo, kb, pdb


def create_payload(key: str, board: str | None = None) -> dict[str, object]:
    return {
        "tool_name": "kanban_create",
        "tool_input": {
            "title": "Issue #138 atomic regression",
            "assignee": "kanban-developer",
            "workspace_kind": "worktree",
            "project": "control-plane",
            "idempotency_key": key,
            "parents": ["t_parent"],
            "board": board,
        },
    }


def run_barrier() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="issue138-atomic-barrier-") as temp:
        root = Path(temp)
        _home, _shared, _repo, kb, _pdb = setup_environment(root)
        guard = load_guard()
        board_path = Path(kb.kanban_db_path(board=None))
        with sqlite3.connect(board_path) as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
                ("t_parent", "done parent", "done", 1),
            )
        verify_started = threading.Event()
        release = threading.Event()
        claim_started = threading.Event()
        claim_done = threading.Event()
        task_ids: list[str] = []
        outcome: dict[str, object] = {}
        original_verify = guard._verify_readback

        def paused_verify(adapter, raw, binding, task_id):
            task_ids.append(task_id)
            verify_started.set()
            if not release.wait(10):
                raise RuntimeError("barrier release timed out")
            return original_verify(adapter, raw, binding, task_id)

        guard._verify_readback = paused_verify

        def run_guard() -> None:
            outcome["guard_rc"] = guard.evaluate_payload(
                create_payload("github:owner/repo:issue:138:atomic-barrier")
            )

        def run_claim() -> None:
            claim_started.set()
            conn = kb.connect(db_path=board_path)
            try:
                claimed = kb.claim_task(conn, task_ids[0], claimer="atomic-barrier-claimer")
                outcome["claimed"] = claimed is not None
            finally:
                conn.close()
                claim_done.set()

        guard_thread = threading.Thread(target=run_guard)
        guard_thread.start()
        assert verify_started.wait(10), "guard did not reach in-transaction read-back"
        task_id = task_ids[0]
        with sqlite3.connect(board_path) as conn:
            before_rows = conn.execute(
                "SELECT id, status FROM tasks WHERE id = ?", (task_id,)
            ).fetchall()
            before_events = conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchall()
        claim_thread = threading.Thread(target=run_claim)
        claim_thread.start()
        assert claim_started.wait(5)
        claim_completed_before_release = claim_done.wait(0.3)
        release.set()
        guard_thread.join(15)
        claim_thread.join(15)
        guard._verify_readback = original_verify
        assert not guard_thread.is_alive()
        assert not claim_thread.is_alive()
        with sqlite3.connect(board_path) as conn:
            after = conn.execute(
                "SELECT status, workspace_kind, workspace_path, branch_name FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            links = [
                tuple(row)
                for row in conn.execute(
                    "SELECT parent_id, child_id FROM task_links WHERE child_id = ?", (task_id,)
                ).fetchall()
            ]
            event_kinds = [
                str(row[0])
                for row in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
                ).fetchall()
            ]
        return {
            "scenario": "barrier",
            "guard_rc": outcome.get("guard_rc"),
            "claimed": outcome.get("claimed"),
            "before_rows": before_rows,
            "before_events": before_events,
            "claim_completed_before_release": claim_completed_before_release,
            "after": after,
            "links": links,
            "event_kinds": event_kinds,
        }


def run_rollback() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="issue138-atomic-rollback-") as temp:
        root = Path(temp)
        _home, _shared, _repo, kb, _pdb = setup_environment(root)
        board_path = Path(kb.kanban_db_path(board=None))
        with sqlite3.connect(board_path) as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
                ("t_parent", "done parent", "done", 1),
            )
        guard = load_guard()
        original_verify = guard._verify_readback

        def force_mismatch(*_args, **_kwargs):
            raise guard.BindingError("forced read-back mismatch")

        guard._verify_readback = force_mismatch
        try:
            rc = guard.evaluate_payload(
                create_payload("github:owner/repo:issue:138:atomic-rollback")
            )
        finally:
            guard._verify_readback = original_verify
        with sqlite3.connect(board_path) as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ?",
                ("github:owner/repo:issue:138:atomic-rollback",),
            ).fetchall()
            links = conn.execute("SELECT * FROM task_links").fetchall()
            events = conn.execute(
                "SELECT * FROM task_events WHERE kind = 'created'"
            ).fetchall()
        return {
            "scenario": "rollback",
            "guard_rc": rc,
            "rows": rows,
            "links": links,
            "created_events": events,
        }


def run_paths() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="issue138-canonical-paths-") as temp:
        root = Path(temp)
        home = root / "profile"
        shared = root / "shared-kanban"
        home.mkdir()
        named = shared / "kanban" / "boards" / "ctrl-hangul"
        named.mkdir(parents=True)
        (named / "board.json").write_text("{}", encoding="utf-8")
        current = shared / "kanban" / "current"
        current.write_text("Ctrl-Hangul\n", encoding="utf-8")
        os.environ.update({"HERMES_HOME": str(home), "HERMES_KANBAN_HOME": str(shared)})
        guard = load_guard()
        from hermes_cli import kanban_db as kb

        cases: list[dict[str, str | None]] = []

        def check(label: str, board: str | None) -> None:
            expected = Path(kb.kanban_db_path(board=board))
            actual = guard._board_db_path(board)
            key = "github:owner/repo:issue:138:path:" + label
            with guard._creation_lock(board, key) as locked:
                assert Path(locked) == actual == expected
            cases.append({"label": label, "path": str(actual)})

        check("explicit-lower", "ctrl-hangul")
        check("explicit-case", "CTRL-HANGUL")
        check("current-pointer", None)
        os.environ["HERMES_KANBAN_BOARD"] = "CTRL-HANGUL"
        check("env-case", None)
        os.environ.pop("HERMES_KANBAN_BOARD", None)
        check("default", "default")
        pinned = root / "pinned.db"
        os.environ["HERMES_KANBAN_DB"] = str(pinned)
        check("pinned", "Ctrl-Hangul")
        assert not (home / "kanban.db").exists()
        assert not (home / "kanban").exists()
        return {
            "scenario": "paths",
            "cases": cases,
            "profile_shadow_exists": (home / "kanban.db").exists()
            or (home / "kanban").exists(),
        }


_SAME_KEY_WORKER = r'''
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

repo_root = Path(sys.argv[1])
ready = Path(sys.argv[2])
release = Path(sys.argv[3])
board_arg = None if sys.argv[4] == "NONE" else sys.argv[4]
key = sys.argv[5]
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15
while not release.exists():
    if time.monotonic() >= deadline:
        raise SystemExit("same-key worker barrier timed out")
    time.sleep(0.01)
payload = {
    "tool_name": "kanban_create",
    "tool_input": {
        "title": "Issue #138 concurrent graph",
        "assignee": "kanban-developer",
        "workspace_kind": "worktree",
        "project": "control-plane",
        "idempotency_key": key,
        "parents": ["t_parent"],
        "board": board_arg,
    },
}
result = subprocess.run(
    [sys.executable, str(repo_root / "automation/hermes/scripts/kanban-block-kind-guard.py")],
    input=json.dumps(payload),
    capture_output=True,
    text=True,
    env=os.environ.copy(),
    check=False,
)
print(
    json.dumps(
        {
            "rc": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    )
)
if result.returncode != 0:
    raise SystemExit(result.returncode)
'''


def run_same_key() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="issue138-same-key-") as temp:
        root = Path(temp)
        _home, _shared, _repo, kb, _pdb = setup_environment(root)
        board_path = Path(kb.kanban_db_path(board=None))
        with sqlite3.connect(board_path) as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
                ("t_parent", "done parent", "done", 1),
            )
        results: list[dict[str, object]] = []
        for suffix, board_args in (
            ("current", ("NONE", "Ctrl-Hangul")),
            ("case", ("ctrl-hangul", "CTRL-HANGUL")),
        ):
            key = f"github:owner/repo:issue:138:same-key:{suffix}"
            ready_paths = [root / f"{suffix}-worker-{index}.ready" for index in range(2)]
            release = root / f"{suffix}.release"
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _SAME_KEY_WORKER,
                        str(repo_root),
                        str(ready_paths[index]),
                        str(release),
                        board_args[index],
                        key,
                    ],
                    env=os.environ.copy(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(2)
            ]
            try:
                deadline = time.monotonic() + 15
                while not all(path.exists() for path in ready_paths):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("same-key workers did not reach the start barrier")
                    time.sleep(0.01)
                release.write_text("release", encoding="utf-8")
                outputs = [process.communicate(timeout=30) for process in processes]
            finally:
                release.touch()
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            for process, (stdout, stderr) in zip(processes, outputs):
                assert process.returncode == 0, stderr or stdout
            with sqlite3.connect(board_path) as conn:
                rows = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT id, status FROM tasks WHERE idempotency_key = ?", (key,)
                    ).fetchall()
                ]
                links = [
                    tuple(row)
                    for row in conn.execute(
                        "SELECT parent_id, child_id FROM task_links "
                        "WHERE child_id IN (SELECT id FROM tasks WHERE idempotency_key = ?)",
                        (key,),
                    ).fetchall()
                ]
                created_events = conn.execute(
                    "SELECT COUNT(*) FROM task_events "
                    "WHERE task_id IN (SELECT id FROM tasks WHERE idempotency_key = ?) "
                    "AND kind = 'created'",
                    (key,),
                ).fetchone()[0]
            results.append(
                {
                    "suffix": suffix,
                    "rows": rows,
                    "links": links,
                    "created_events": created_events,
                }
            )
        return {"scenario": "same-key", "results": results}


def run_lifecycle() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="issue138-lifecycle-replay-") as temp:
        root = Path(temp)
        _home, _shared, _repo, kb, _pdb = setup_environment(root)
        board_path = Path(kb.kanban_db_path(board=None))
        with sqlite3.connect(board_path) as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
                ("t_parent", "done parent", "done", 1),
            )
        guard = load_guard()
        def runtime_state() -> tuple[tuple[object, ...], list[tuple[object, ...]]]:
            with sqlite3.connect(board_path) as conn:
                task = conn.execute(
                    "SELECT status, claim_lock, claim_expires, worker_pid, current_run_id, "
                    "max_runtime_seconds, last_heartbeat_at FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                runs = conn.execute(
                    "SELECT id, status, claim_lock, claim_expires, worker_pid, "
                    "last_heartbeat_at, outcome, summary, metadata, error "
                    "FROM task_runs WHERE task_id = ? ORDER BY id",
                    (task_id,),
                ).fetchall()
            return task, runs

        key = "github:owner/repo:issue:138:lifecycle-replay"
        payload = create_payload(key)
        first_rc = guard.evaluate_payload(payload)
        with sqlite3.connect(board_path) as conn:
            task_id = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ?", (key,)
            ).fetchone()[0]
        conn = kb.connect(db_path=board_path)
        try:
            claimed = kb.claim_task(conn, task_id, claimer="lifecycle-replay-claimer")
        finally:
            conn.close()
        before_running, before_runs = runtime_state()
        running_rc = guard.evaluate_payload(payload)
        after_running, after_runs = runtime_state()
        with sqlite3.connect(board_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = 'review', claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL, current_run_id = NULL WHERE id = ?",
                (task_id,),
            )
            conn.commit()
        review_rc = guard.evaluate_payload(payload)
        review_state, review_runs = runtime_state()
        with sqlite3.connect(board_path) as conn:
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
            conn.commit()
        done_rc = guard.evaluate_payload(payload)
        done_state, done_runs = runtime_state()
        return {
            "scenario": "lifecycle",
            "first_rc": first_rc,
            "claimed": claimed is not None,
            "before_running": before_running,
            "running_rc": running_rc,
            "after_running": after_running,
            "before_runs": before_runs,
            "after_runs": after_runs,
            "review_rc": review_rc,
            "review_state": review_state,
            "review_runs": review_runs,
            "done_rc": done_rc,
            "done_state": done_state,
            "done_runs": done_runs,
        }


def run_duplicates() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="issue138-legacy-duplicates-") as temp:
        root = Path(temp)
        _home, _shared, _repo, kb, _pdb = setup_environment(root)
        board_path = Path(kb.kanban_db_path(board=None))
        unique_key = "github:owner/repo:issue:138:legacy-unique-real"
        tied_key = "github:owner/repo:issue:138:legacy-tied-real"
        with sqlite3.connect(board_path) as conn:
            conn.executemany(
                "INSERT INTO tasks (id, title, status, idempotency_key, created_at) "
                "VALUES (?, ?, 'ready', ?, ?)",
                [
                    ("t_unique_old", "old", unique_key, 10),
                    ("t_unique_new", "new", unique_key, 20),
                    ("t_tied_a", "a", tied_key, 30),
                    ("t_tied_b", "b", tied_key, 30),
                ],
            )
            core_unique = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
                "ORDER BY created_at DESC LIMIT 1",
                (unique_key,),
            ).fetchone()[0]
        guard = load_guard()
        adapter = guard._open_adapter(None, db_path=board_path)
        try:
            guard_unique = adapter.find_idempotent(unique_key)
            try:
                adapter.find_idempotent(tied_key)
            except guard.BindingError as exc:
                tied_error = type(exc).__name__
            else:
                tied_error = None
        finally:
            adapter.close()
        tied_rc = guard.evaluate_payload(create_payload(tied_key))
        with sqlite3.connect(board_path) as conn:
            tied_rows = conn.execute(
                "SELECT id, status FROM tasks WHERE idempotency_key = ? ORDER BY id", (tied_key,)
            ).fetchall()
            quarantine_events = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE kind = 'workspace_binding_quarantined'"
            ).fetchone()[0]
        return {
            "scenario": "duplicates",
            "core_unique": core_unique,
            "guard_unique": guard_unique,
            "tied_error": tied_error,
            "tied_rc": tied_rc,
            "tied_rows": tied_rows,
            "quarantine_events": quarantine_events,
        }


if scenario == "barrier":
    print(json.dumps(run_barrier(), default=list))
elif scenario == "rollback":
    print(json.dumps(run_rollback(), default=list))
elif scenario == "paths":
    print(json.dumps(run_paths(), default=list))
elif scenario == "same-key":
    print(json.dumps(run_same_key(), default=list))
elif scenario == "lifecycle":
    print(json.dumps(run_lifecycle(), default=list))
elif scenario == "duplicates":
    print(json.dumps(run_duplicates(), default=list))
else:
    raise SystemExit(f"unknown scenario: {scenario}")
"""


def _run_real_atomic_regression(scenario: str) -> dict[str, Any]:
    assert HERMES_PYTHON.is_file(), f"Hermes test runtime is missing: {HERMES_PYTHON}"
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    result = subprocess.run(
        [str(HERMES_PYTHON), "-c", _REAL_ATOMIC_REGRESSION, str(ROOT), scenario],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("attempt", [1, 2])
def test_real_core_create_claim_barrier_hides_uncommitted_graph(attempt: int) -> None:
    del attempt
    result = _run_real_atomic_regression("barrier")
    assert result["guard_rc"] == 0, result
    assert result["claimed"] is True, result
    assert result["before_rows"] == [], result
    assert result["before_events"] == [], result
    assert result["claim_completed_before_release"] is False, result
    assert result["after"] and result["after"][0] == "running", result
    assert len(result["links"]) == 1, result
    assert result["links"][0][0] == "t_parent", result


@pytest.mark.parametrize("attempt", [1, 2])
def test_real_core_readback_mismatch_rolls_back_new_graph(attempt: int) -> None:
    del attempt
    result = _run_real_atomic_regression("rollback")
    assert result["guard_rc"] == 2, result
    assert result["rows"] == [], result
    assert result["links"] == [], result
    assert result["created_events"] == [], result


@pytest.mark.parametrize("attempt", [1, 2])
def test_real_core_resolver_controls_creation_lock_without_profile_shadow(attempt: int) -> None:
    del attempt
    result = _run_real_atomic_regression("paths")
    assert result["profile_shadow_exists"] is False, result
    cases = {item["label"]: item["path"] for item in result["cases"]}
    assert cases["explicit-lower"] == cases["explicit-case"], result
    assert cases["explicit-lower"] == cases["current-pointer"], result
    assert cases["current-pointer"] == cases["env-case"], result
    assert cases["default"].endswith("/shared-kanban/kanban.db"), result
    assert cases["pinned"].endswith("/pinned.db"), result


@pytest.mark.parametrize("attempt", [1, 2])
def test_real_core_concurrent_same_key_requests_share_one_canonical_graph(attempt: int) -> None:
    del attempt
    result = _run_real_atomic_regression("same-key")
    assert len(result["results"]) == 2, result
    for item in result["results"]:
        assert len(item["rows"]) == 1, result
        assert item["rows"][0][1] == "ready", result
        assert item["links"] == [["t_parent", item["rows"][0][0]]], result
        assert item["created_events"] == 1, result


def test_invalid_incident_payload_blocks_before_any_row(fixture: dict[str, Any]) -> None:
    guard = fixture["guard"]
    raw = _valid(fixture["repo"])
    raw.update(workspace_kind="dir", workspace_path=str(fixture["repo"]))
    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 2
    assert _count_tasks(fixture["board"]) == 0
    assert fixture["adapters"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("workspace_kind", "scratch"),
        ("workspace_kind", "dir"),
        ("workspace_path", None),
        ("workspace_path", "outside-worktrees"),
        ("project", "missing-project"),
        ("idempotency_key", None),
        ("branch_name", "shared-branch"),
        ("branch_name", None),
    ),
)
def test_binding_matrix_fails_closed_without_materialization(
    fixture: dict[str, Any], field: str, value: Any
) -> None:
    guard = fixture["guard"]
    raw = _valid(fixture["repo"], key=f"github:owner/repo:issue:138:{field}")
    if field == "idempotency_key" and value is None:
        raw.pop(field)
    elif field == "workspace_path" and value is None:
        raw[field] = None
    elif field == "workspace_path":
        raw[field] = str(fixture["repo"] / "outside-worktree")
    else:
        raw[field] = value
    if field == "workspace_kind" and value == "dir":
        raw["workspace_path"] = str(fixture["repo"])
    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 2
    assert _count_tasks(fixture["board"]) == 0
    assert fixture["adapters"] == []


def test_missing_repository_anchor_fails_closed(fixture: dict[str, Any]) -> None:
    conn = sqlite3.connect(Path(fixture["guard"]._project_db_path()))
    conn.execute("UPDATE projects SET primary_path = ?", (str(Path("/missing/repo")),))
    conn.commit()
    conn.close()
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "kanban_create", "tool_input": _valid(fixture["repo"])}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0


def test_valid_creation_reads_back_binding_status_and_parent_graph(fixture: dict[str, Any]) -> None:
    guard = fixture["guard"]
    conn = sqlite3.connect(fixture["board"])
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status) VALUES (?, ?, ?, ?)",
        ("t_parent", "developer", "kanban-developer", "done"),
    )
    conn.commit()
    conn.close()
    raw = _valid(fixture["repo"])
    raw["assignee"] = "kanban-reviewer"
    raw["parents"] = ["t_parent"]
    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 0
    conn = sqlite3.connect(fixture["board"])
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM tasks WHERE idempotency_key = ?", (raw["idempotency_key"],)
    ).fetchone()
    assert row is not None
    assert row["status"] == "ready"
    assert row["workspace_kind"] == "worktree"
    assert row["workspace_path"] == str(fixture["repo"] / ".worktrees" / row["id"])
    assert row["branch_name"].startswith(f"control-plane/{row['id']}-")
    links = conn.execute(
        "SELECT parent_id, child_id FROM task_links WHERE child_id = ?", (row["id"],)
    ).fetchall()
    assert [tuple(link) for link in links] == [("t_parent", row["id"])]
    conn.close()


@pytest.mark.parametrize("status", ["running", "review", "done"])
def test_exact_key_replay_preserves_normal_lifecycle_and_claim_metadata(
    fixture: dict[str, Any], status: str
) -> None:
    guard = fixture["guard"]
    raw = _valid(fixture["repo"], key=f"github:owner/repo:issue:138:replay:{status}")
    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 0
    conn = sqlite3.connect(fixture["board"])
    conn.execute(
        "UPDATE tasks SET status = ?, claim_lock = ?, claim_expires = ?, worker_pid = ?, "
        "current_run_id = ? WHERE idempotency_key = ?",
        (
            status,
            "claim-replay" if status == "running" else None,
            9999999999 if status == "running" else None,
            4242 if status == "running" else None,
            7 if status == "running" else None,
            raw["idempotency_key"],
        ),
    )
    conn.commit()
    before = conn.execute(
        "SELECT id, status, claim_lock, claim_expires, worker_pid, current_run_id "
        "FROM tasks WHERE idempotency_key = ?",
        (raw["idempotency_key"],),
    ).fetchone()
    conn.close()

    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 0

    conn = sqlite3.connect(fixture["board"])
    after = conn.execute(
        "SELECT id, status, claim_lock, claim_expires, worker_pid, current_run_id "
        "FROM tasks WHERE idempotency_key = ?",
        (raw["idempotency_key"],),
    ).fetchone()
    event_count = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'workspace_binding_quarantined'",
        (before[0],),
    ).fetchone()[0]
    conn.close()
    assert after == before
    assert event_count == 0


def test_unique_legacy_duplicate_selection_matches_core_newest_timestamp(
    fixture: dict[str, Any],
) -> None:
    guard = fixture["guard"]
    raw = _valid(fixture["repo"], key="github:owner/repo:issue:138:legacy-unique")
    binding = guard.RepoBinding("p_control", "control-plane", fixture["repo"])
    conn = sqlite3.connect(fixture["board"])
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, workspace_kind, workspace_path, "
        "branch_name, project_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_legacy_old",
            "wrong old row",
            raw["assignee"],
            "ready",
            "scratch",
            str(fixture["repo"]),
            "wrong-branch",
            "p_control",
            raw["idempotency_key"],
            10,
        ),
    )
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, workspace_kind, workspace_path, "
        "branch_name, project_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_legacy_new",
            raw["title"],
            raw["assignee"],
            "ready",
            "worktree",
            str(fixture["repo"] / ".worktrees" / "t_legacy_new"),
            guard._branch_name(binding, "t_legacy_new", raw["title"]),
            "p_control",
            raw["idempotency_key"],
            20,
        ),
    )
    conn.commit()
    conn.close()

    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 0
    conn = sqlite3.connect(fixture["board"])
    assert conn.execute("SELECT status FROM tasks WHERE id = 't_legacy_old'").fetchone()[0] == "ready"
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'workspace_binding_quarantined'"
    ).fetchone()[0] == 0
    conn.close()


def test_tied_legacy_duplicate_selection_fails_closed_without_mutation(
    fixture: dict[str, Any],
) -> None:
    guard = fixture["guard"]
    key = "github:owner/repo:issue:138:legacy-tied"
    conn = sqlite3.connect(fixture["board"])
    conn.executemany(
        "INSERT INTO tasks (id, title, status, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?)",
        [
            ("t_legacy_a", "legacy a", "ready", key, 30),
            ("t_legacy_b", "legacy b", "ready", key, 30),
        ],
    )
    conn.commit()
    conn.close()
    raw = _valid(fixture["repo"], key=key)
    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 2
    conn = sqlite3.connect(fixture["board"])
    assert conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ? ORDER BY id", (key,)
    ).fetchall() == [("t_legacy_a", "ready"), ("t_legacy_b", "ready")]
    assert conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = 'workspace_binding_quarantined'"
    ).fetchone()[0] == 0
    conn.close()


def test_open_parent_keeps_valid_child_on_todo_dependency_path(fixture: dict[str, Any]) -> None:
    guard = fixture["guard"]
    conn = sqlite3.connect(fixture["board"])
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status) VALUES (?, ?, ?, ?)",
        ("t_open_parent", "developer", "kanban-developer", "running"),
    )
    conn.commit()
    conn.close()
    raw = _valid(fixture["repo"], key="github:owner/repo:issue:138:round:todo")
    raw["parents"] = ["t_open_parent"]
    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 0
    conn = sqlite3.connect(fixture["board"])
    status = conn.execute(
        "SELECT status FROM tasks WHERE idempotency_key = ?", (raw["idempotency_key"],)
    ).fetchone()[0]
    conn.close()
    assert status == "todo"


@pytest.mark.parametrize("corrupt_field", ["workspace_kind", "workspace_path", "branch_name"])
def test_new_durable_binding_mismatch_rolls_back_without_a_dispatchable_row(
    fixture: dict[str, Any], corrupt_field: str
) -> None:
    guard = fixture["guard"]

    def corrupt_open(_: str | None, *, db_path: Path | None = None) -> FakeAdapter:
        del db_path
        adapter = FakeAdapter(guard, fixture["board"], corrupt_field=corrupt_field)
        fixture["adapters"].append(adapter)
        return adapter

    original = guard._open_adapter
    guard._open_adapter = corrupt_open
    try:
        raw = _valid(fixture["repo"], key=f"github:owner/repo:issue:138:corrupt:{corrupt_field}")
        assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 2
    finally:
        guard._open_adapter = original

    conn = sqlite3.connect(fixture["board"])
    row = conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ?",
        (raw["idempotency_key"],),
    ).fetchone()
    links = conn.execute("SELECT * FROM task_links").fetchall()
    events = conn.execute(
        "SELECT * FROM task_events WHERE kind = 'workspace_binding_quarantined'"
    ).fetchall()
    conn.close()
    assert row is None
    assert links == []
    assert events == []


def test_existing_durable_binding_mismatch_uses_nonrunning_cas_quarantine(
    fixture: dict[str, Any],
) -> None:
    guard = fixture["guard"]
    raw = _valid(fixture["repo"], key="github:owner/repo:issue:138:existing-invalid")
    conn = sqlite3.connect(fixture["board"])
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, workspace_kind, workspace_path, "
        "branch_name, project_id, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_existing_invalid",
            raw["title"],
            raw["assignee"],
            "ready",
            "scratch",
            str(fixture["repo"] / ".worktrees" / "t_existing_invalid"),
            guard._branch_name(
                guard.RepoBinding("p_control", "control-plane", fixture["repo"]),
                "t_existing_invalid",
                raw["title"],
            ),
            "p_control",
            raw["idempotency_key"],
        ),
    )
    conn.commit()
    conn.close()

    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 2

    conn = sqlite3.connect(fixture["board"])
    row = conn.execute(
        "SELECT status, claim_lock, claim_expires, worker_pid, current_run_id "
        "FROM tasks WHERE id = ?",
        ("t_existing_invalid",),
    ).fetchone()
    event = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ?", ("t_existing_invalid",)
    ).fetchone()
    conn.close()
    assert row == ("blocked", None, None, None, None)
    assert event == ("workspace_binding_quarantined",)


def test_existing_running_binding_mismatch_is_not_overwritten(
    fixture: dict[str, Any],
) -> None:
    guard = fixture["guard"]
    raw = _valid(fixture["repo"], key="github:owner/repo:issue:138:existing-running")
    conn = sqlite3.connect(fixture["board"])
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, workspace_kind, workspace_path, "
        "branch_name, project_id, idempotency_key, claim_lock, claim_expires, worker_pid, "
        "current_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "t_existing_running",
            raw["title"],
            raw["assignee"],
            "running",
            "scratch",
            str(fixture["repo"]),
            "shared-branch",
            "p_control",
            raw["idempotency_key"],
            "active-claim",
            9999999999,
            4242,
            7,
        ),
    )
    conn.commit()
    conn.close()

    assert guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 2

    conn = sqlite3.connect(fixture["board"])
    row = conn.execute(
        "SELECT status, workspace_kind, workspace_path, branch_name, claim_lock, "
        "claim_expires, worker_pid, current_run_id FROM tasks WHERE id = ?",
        ("t_existing_running",),
    ).fetchone()
    event_count = conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", ("t_existing_running",)
    ).fetchone()[0]
    conn.close()
    assert row == (
        "running",
        "scratch",
        str(fixture["repo"]),
        "shared-branch",
        "active-claim",
        9999999999,
        4242,
        7,
    )
    assert event_count == 0


def test_explicit_blocked_quarantine_is_non_dispatchable_and_exempt(fixture: dict[str, Any]) -> None:
    raw = {
        "title": "operator safety quarantine",
        "assignee": "kanban-developer",
        "workspace_kind": "scratch",
        "initial_status": "blocked",
    }
    assert fixture["guard"].evaluate_payload({"tool_name": "kanban_create", "tool_input": raw}) == 0
    assert fixture["adapters"] == []


def test_barrier_concurrent_same_round_requests_converge_to_one_task(fixture: dict[str, Any]) -> None:
    guard = fixture["guard"]
    raw = _valid(fixture["repo"], key="github:owner/repo:issue:138:round:2")
    barrier = threading.Barrier(2)

    def run() -> int:
        barrier.wait(timeout=5)
        return guard.evaluate_payload({"tool_name": "kanban_create", "tool_input": raw})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results == [0, 0]
    conn = sqlite3.connect(fixture["board"])
    rows = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ?", (raw["idempotency_key"],)
    ).fetchall()
    conn.close()
    assert len(rows) == 1


def test_terminal_create_uses_the_same_binding_preflight(fixture: dict[str, Any]) -> None:
    command = (
        "hermes kanban create 'Issue #138 terminal developer' "
        "--assignee kanban-developer --workspace worktree --project control-plane "
        "--idempotency-key github:owner/repo:issue:138:round:terminal"
    )
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 0
    assert _count_tasks(fixture["board"]) == 1


def test_terminal_create_preserves_authoritative_cli_fields(fixture: dict[str, Any]) -> None:
    command = (
        "hermes kanban create 'Issue #138 cli surface' --assignee kanban-developer "
        "--workspace worktree --project control-plane "
        "--idempotency-key github:owner/repo:issue:138:cli-surface "
        "--skill translation --skill github-code-review --max-retries 3 "
        "--created-by cli-author --goal --goal-max-turns 7 --max-runtime 30m"
    )
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 0
    conn = sqlite3.connect(fixture["board"])
    row = conn.execute(
        "SELECT created_by, max_runtime_seconds, skills, max_retries, goal_mode, goal_max_turns "
        "FROM tasks WHERE idempotency_key = ?",
        ("github:owner/repo:issue:138:cli-surface",),
    ).fetchone()
    conn.close()
    assert row == ("cli-author", 1800, '["translation", "github-code-review"]', 3, 1, 7)


@pytest.mark.parametrize("bad_option", ["--goal-mode", "--unknown-create-option"])
def test_terminal_create_rejects_options_the_authoritative_cli_rejects(
    fixture: dict[str, Any], bad_option: str
) -> None:
    command = (
        "hermes kanban create invalid --assignee kanban-developer --workspace worktree "
        "--project control-plane --idempotency-key github:owner/repo:issue:138:bad-option "
        f"{bad_option}"
    )
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0


@pytest.mark.parametrize(
    ("prefix", "expected_count"),
    [("false &&", 0), ("true ||", 0), ("true &&", 1), ("false ||", 1)],
)
def test_terminal_literal_short_circuit_controls_materialization(
    fixture: dict[str, Any], prefix: str, expected_count: int
) -> None:
    key = f"github:owner/repo:issue:138:short-circuit:{prefix.replace(' ', '-')}"
    command = (
        f"{prefix} hermes kanban create short-circuit --assignee kanban-developer "
        f"--workspace worktree --project control-plane --idempotency-key {key}"
    )
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 0
    assert _count_tasks(fixture["board"]) == expected_count


@pytest.mark.parametrize("operator", ["&", "|", ";&", ";;&", "|||"])
@pytest.mark.parametrize("prefix", ["false &&", "true ||"])
def test_terminal_unsupported_shell_operators_fail_closed_without_mutation(
    fixture: dict[str, Any], operator: str, prefix: str
) -> None:
    create = (
        "hermes kanban create unsupported-one --assignee kanban-developer "
        "--workspace worktree --project control-plane "
        "--idempotency-key github:owner/repo:issue:138:unsupported-one"
    )
    later_create = (
        "hermes kanban create unsupported-two --assignee kanban-developer "
        "--workspace worktree --project control-plane "
        "--idempotency-key github:owner/repo:issue:138:unsupported-two"
    )
    command = f"{prefix} {create} {operator} {later_create}"
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0
    assert fixture["adapters"] == []


@pytest.mark.parametrize("operator", ["&", "|", ";&", ";;&", "|||"])
def test_terminal_unsupported_shell_operators_fail_closed_for_reassign_without_mutation(
    fixture: dict[str, Any], operator: str
) -> None:
    create = (
        "hermes kanban create unsupported-reassign-one --assignee kanban-developer "
        "--workspace worktree --project control-plane "
        "--idempotency-key github:owner/repo:issue:138:unsupported-reassign-one"
    )
    reassign = "hermes kanban reassign t_missing kanban-reviewer --reclaim"
    command = f"false && {create} {operator} {reassign}"
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0
    assert fixture["adapters"] == []


def test_terminal_ambiguous_conditional_fails_closed_before_materialization(
    fixture: dict[str, Any],
) -> None:
    command = (
        "test -f /tmp/maybe && hermes kanban create ambiguous --assignee kanban-developer "
        "--workspace worktree --project control-plane --idempotency-key "
        "github:owner/repo:issue:138:ambiguous"
    )
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0


def test_terminal_ambiguous_conditional_reassign_fails_closed_before_materialization(
    fixture: dict[str, Any],
) -> None:
    command = "if true; then hermes kanban reassign t_missing kanban-reviewer --reclaim; fi"
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0
    assert fixture["adapters"] == []


def test_terminal_multi_create_validates_all_bindings_before_materialization(
    fixture: dict[str, Any],
) -> None:
    command = (
        "hermes kanban create valid --assignee kanban-developer --workspace worktree "
        "--project control-plane --idempotency-key github:owner/repo:issue:138:round:valid "
        "&& hermes kanban create invalid --assignee kanban-reviewer"
    )
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0


def test_stable_hook_blocks_incident_shape_without_touching_core() -> None:
    raw = {
        "tool_name": "kanban_create",
        "tool_input": {
            "title": "Issue #138 R7 developer",
            "assignee": "kanban-developer",
            "workspace_kind": "dir",
            "workspace_path": "/shared/checkout",
            "idempotency_key": "github:owner/repo:issue:138:round:7",
        },
    }
    result = subprocess.run(
        [sys.executable, str(STABLE_GUARD)],
        input=json.dumps(raw),
        text=True,
        capture_output=True,
        env={**os.environ, "KANBAN_WORKSPACE_BINDING_GUARD_LOG": str(Path(tempfile.gettempdir()) / "workspace-binding-test.log")},
        check=False,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["action"] == "block"


@pytest.mark.parametrize("operator", ["&", "|", ";&", ";;&", "|||"])
def test_stable_hook_rejects_unsupported_operator_before_materialization(operator: str) -> None:
    raw = {
        "tool_name": "terminal",
        "tool_input": {
            "command": (
                "false && hermes kanban create unsupported-one "
                "--assignee kanban-developer --workspace worktree "
                "--project control-plane "
                "--idempotency-key github:owner/repo:issue:138:stable-one "
                f"{operator} hermes kanban create unsupported-two "
                "--assignee kanban-developer --workspace worktree "
                "--project control-plane "
                "--idempotency-key github:owner/repo:issue:138:stable-two"
            )
        },
    }
    with tempfile.TemporaryDirectory(prefix="stable-unsupported-operator-") as directory:
        result = subprocess.run(
            [sys.executable, str(STABLE_GUARD)],
            input=json.dumps(raw),
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "KANBAN_SPECIALIST_COMPLETION_GUARD_LOG": str(
                    Path(directory) / "specialist.log"
                ),
                "KANBAN_WORKSPACE_BINDING_GUARD_LOG": str(Path(directory) / "workspace.log"),
            },
            check=False,
        )
    assert result.returncode == 2, (result.stdout, result.stderr)
    body = json.loads(result.stdout)
    assert body["action"] == "block"
    assert "unsupported shell operator" in body["message"]
    assert "No task mutation was performed" in body["message"]


@pytest.mark.parametrize("operator", ["&", "|", ";&", ";;&", "|||"])
def test_stable_hook_rejects_unsupported_reassign_before_materialization(
    fixture: dict[str, Any], operator: str
) -> None:
    raw = {
        "tool_name": "terminal",
        "tool_input": {
            "command": (
                "false && hermes kanban create unsupported-reassign-one "
                "--assignee kanban-developer --workspace worktree "
                "--project control-plane "
                "--idempotency-key github:owner/repo:issue:138:stable-reassign-one "
                f"{operator} hermes kanban reassign t_missing kanban-reviewer --reclaim"
            )
        },
    }
    result = subprocess.run(
        [sys.executable, str(STABLE_GUARD)],
        input=json.dumps(raw),
        text=True,
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    body = json.loads(result.stdout)
    assert body["action"] == "block"
    assert "unsupported shell operator" in body["message"]
    assert "No task mutation was performed" in body["message"]
    assert _count_tasks(fixture["board"]) == 0


def test_stable_hook_rejects_ambiguous_conditional_reassign_before_materialization(
    fixture: dict[str, Any],
) -> None:
    raw = {
        "tool_name": "terminal",
        "tool_input": {
            "command": "if true; then hermes kanban reassign t_missing kanban-reviewer --reclaim; fi"
        },
    }
    result = subprocess.run(
        [sys.executable, str(STABLE_GUARD)],
        input=json.dumps(raw),
        text=True,
        capture_output=True,
        env=os.environ.copy(),
        check=False,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    body = json.loads(result.stdout)
    assert body["action"] == "block"
    assert "conditional" in body["message"]
    assert "No task mutation was performed" in body["message"]
    assert _count_tasks(fixture["board"]) == 0


def test_padded_idempotency_key_fails_closed_before_materialization(
    fixture: dict[str, Any],
) -> None:
    raw = _valid(
        fixture["repo"],
        key=" github:owner/repo:issue:138:round:padded ",
    )
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "kanban_create", "tool_input": raw}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0
    assert fixture["adapters"] == []


@pytest.mark.parametrize(
    "shell_token",
    ["$KEY", "${KEY}", "$(printf expanded)", "`printf expanded`", "$((1 + 1))"],
)
def test_terminal_shell_substitution_fails_closed_before_materialization(
    fixture: dict[str, Any], shell_token: str
) -> None:
    command = (
        "sh -c 'hermes kanban create shell-token --assignee kanban-developer "
        "--workspace worktree --project control-plane --idempotency-key "
        f"{shell_token}'"
    )
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0
    assert fixture["adapters"] == []


@pytest.mark.parametrize(
    "command",
    [
        "hermes kanban create $TITLE --assignee kanban-developer --workspace worktree "
        "--project control-plane --idempotency-key exact-title",
        "hermes kanban create body --assignee kanban-developer --workspace worktree "
        "--project control-plane --body '${BODY}' --idempotency-key exact-body",
        "hermes kanban create project --assignee kanban-developer --workspace worktree "
        "--project '$(printf project)' --idempotency-key exact-project",
        "hermes kanban create key --assignee kanban-developer --workspace worktree "
        "--project control-plane --idempotency-key '`printf key`'",
        "hermes kanban create assignee --assignee '$PROFILE' --workspace worktree "
        "--project control-plane --idempotency-key exact-assignee",
        "hermes kanban create process --assignee kanban-developer --workspace worktree "
        "--project control-plane --idempotency-key '<(printf process)'",
        "hermes kanban --board '$BOARD' create board --assignee kanban-developer "
        "--workspace worktree --project control-plane --idempotency-key exact-board",
    ],
)
def test_terminal_substitution_is_rejected_in_every_create_argument(
    fixture: dict[str, Any], command: str
) -> None:
    assert fixture["guard"].evaluate_payload(
        {"tool_name": "terminal", "tool_input": {"command": command}}
    ) == 2
    assert _count_tasks(fixture["board"]) == 0
    assert fixture["adapters"] == []


def test_real_guard_and_handler_converge_padded_and_exact_replays() -> None:
    result = _run_real_handler_regression("padded")
    assert result["padded_rc"] == 2
    assert result["after_padded"] == []
    assert result["first_guard_rc"] == 0
    assert result["replay_guard_rc"] == 0
    first = result["first_handler"]
    replay = result["replay_handler"]
    assert isinstance(first, dict) and isinstance(replay, dict)
    assert first["ok"] is True
    assert replay["ok"] is True
    assert first["task_id"] == replay["task_id"]
    assert result["rows"] and len(result["rows"]) == 1
    assert result["rows"][0][1] == "github:owner/repo:issue:138:real-padded"
    assert result["rows"][0][2] == "ready"


def test_real_shell_wrapped_guard_blocks_literal_before_handler() -> None:
    result = _run_real_handler_regression("shell")
    assert result["shell_rc"] == 2
    handler = result["handler"]
    assert isinstance(handler, dict) and handler["ok"] is True
    assert result["rows"] and len(result["rows"]) == 1
    assert result["rows"][0][1] == "github:owner/repo:issue:138:real-shell"
    assert "$KEY" not in {row[1] for row in result["rows"]}
    assert result["rows"][0][2] == "ready"


def test_real_cli_and_core_preserve_the_full_terminal_create_surface() -> None:
    result = _run_real_handler_regression("cli-surface")
    assert result["shell_rc"] == 0
    assert result["cli_rc"] == 0
    assert result["row"] == [
        "cli-author",
        1800,
        '["translation", "github-code-review"]',
        3,
        1,
        7,
    ]
    assert result["structured_guard_rc"] == 0
    assert result["structured_row"] == [
        1800,
        '["translation", "github-code-review"]',
        1,
        7,
    ]


def test_real_replay_accepts_running_review_and_done_without_mutation() -> None:
    result = _run_real_atomic_regression("lifecycle")
    assert result["first_rc"] == 0
    assert result["claimed"] is True
    assert result["running_rc"] == 0
    assert result["before_running"] == result["after_running"]
    assert result["before_runs"] == result["after_runs"]
    assert result["review_rc"] == 0
    assert result["review_runs"] == result["after_runs"]
    assert result["done_rc"] == 0
    assert result["done_runs"] == result["after_runs"]


def test_real_lookup_matches_core_for_unique_legacy_rows_and_fails_tied_rows_closed() -> None:
    result = _run_real_atomic_regression("duplicates")
    assert result == {
        "scenario": "duplicates",
        "core_unique": "t_unique_new",
        "guard_unique": "t_unique_new",
        "tied_error": "BindingError",
        "tied_rc": 2,
        "tied_rows": [["t_tied_a", "ready"], ["t_tied_b", "ready"]],
        "quarantine_events": 0,
    }
