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
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        return None if row is None else str(row["id"])

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
            "branch_name, project_id, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        for parent in parents:
            self.conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent, task_id),
            )
        self.conn.commit()
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

    def quarantine(self, task_id: str, reason: str) -> None:
        self.conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
        self.conn.commit()

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
        "idempotency_key TEXT)"
    )
    conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))
    monkeypatch.setenv("KANBAN_WORKSPACE_BINDING_GUARD_LOG", str(tmp_path / "guard.log"))
    adapters: list[FakeAdapter] = []

    def open_adapter(_: str | None) -> FakeAdapter:
        adapter = FakeAdapter(guard, board)
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr(guard, "_open_adapter", open_adapter)
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

import json
import os
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

    def handler(key: str) -> dict[str, object]:
        from tools import kanban_tools as kt

        return json.loads(
            kt._handle_create(
                {
                    "title": "Issue #138 real handler",
                    "assignee": "kanban-developer",
                    "workspace_kind": "worktree",
                    "project": "control-plane",
                    "idempotency_key": key,
                }
            )
        )

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
def test_durable_binding_mismatch_is_quarantined_before_dispatch(
    fixture: dict[str, Any], corrupt_field: str
) -> None:
    guard = fixture["guard"]

    def corrupt_open(_: str | None) -> FakeAdapter:
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
        "SELECT status FROM tasks WHERE idempotency_key = ?",
        (raw["idempotency_key"],),
    ).fetchone()
    conn.close()
    assert row == ("blocked",)


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
