"""Process regression for completion wake loss behind the canonical edge lock."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = ROOT / "hermes-plugin" / "github-completion-edge-wake" / "__init__.py"

_EDGE_STUB = r'''
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

state_path = Path(os.environ["EDGE_RETRY_STATE"])
hermes_home = Path(os.environ["HERMES_HOME"])
lock_dir = hermes_home / "kanban" / ".resource-locks"
lock_dir.mkdir(parents=True, exist_ok=True)
lock_path = lock_dir / "github-edge-sync.lock"
role = os.environ["EDGE_ROLE"]
attempt_marker = Path(os.environ["EDGE_COMPLETION_ATTEMPT_MARKER"])
attempt_log = Path(os.environ["EDGE_COMPLETION_ATTEMPT_LOG"])


def update_state(event: str) -> None:
    with state_path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = json.load(handle)
        state["events"].append(event)
        if event.startswith("start:"):
            state["snapshots"].append(
                {
                    "role": role,
                    "completion_committed": bool(state["completion_committed"]),
                }
            )
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# Record every completion child before it blocks on the canonical edge lock.
# This makes the regression prove that a first child really existed and later
# timed out instead of merely passing because process startup was slow.
if role == "completion":
    with attempt_log.open("a", encoding="utf-8") as handle:
        handle.write("attempt\n")
        handle.flush()
        os.fsync(handle.fileno())
    attempt_marker.write_text("started", encoding="utf-8")

with lock_path.open("a+b") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    update_state(f"start:{role}")
    if role == "owner":
        Path(os.environ["EDGE_OWNER_STARTED"]).write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 8.0
        while not attempt_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not attempt_marker.exists():
            raise SystemExit("completion attempt did not reach lock contention")
        # The first completion attempt's parent budget is 3.0s. Hold the lock
        # beyond that budget *from the observed attempt start*, then release
        # early enough for the fresh second attempt to acquire and run with its
        # own full 3.0s budget even on slower CI/container process startup.
        time.sleep(float(os.environ["EDGE_OWNER_POST_ATTEMPT_HOLD"]))
    else:
        time.sleep(float(os.environ["EDGE_COMPLETION_HOLD"]))
    update_state(f"end:{role}")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

print(json.dumps({"role": role, "argv": sys.argv[1:]}))
'''

_PLUGIN_DRIVER = r'''
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

source = Path(os.environ["PLUGIN_SOURCE"])
spec = importlib.util.spec_from_file_location("completion_retry_plugin", source)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

if not os.environ.get("REAL_COMPLETION_ELIGIBILITY"):
    plugin._completion_is_eligible = lambda task_id, board: True
plugin._edge_script_path = lambda: Path(os.environ["EDGE_STUB"])
plugin._EDGE_TIMEOUT_GRACE_SECONDS = 0.0
original_run_edge = plugin._run_edge
attempt_log = os.environ.get("EDGE_ATTEMPT_LOG")


def run_edge(edge_path, board):
    if attempt_log:
        with Path(attempt_log).open("a", encoding="utf-8") as handle:
            handle.write("attempt\n")
    return original_run_edge(edge_path, board)


plugin._run_edge = run_edge
diagnostics = []
plugin._diagnostic = lambda task_id, board, code: diagnostics.append(
    {"task_id": task_id, "board": board, "code": code}
)
plugin._on_task_completed(
    task_id=os.environ.get("EDGE_TASK_ID", "t_12345678"),
    board="default",
)
print(json.dumps({"diagnostics": diagnostics}))
'''

_REAL_EDGE_DRIVER = r'''
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

source = Path(os.environ["EDGE_HARNESS"])
spec = importlib.util.spec_from_file_location("contention_edge_fixture", source)
assert spec is not None and spec.loader is not None
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)

role = os.environ["EDGE_ROLE"]
task_id = os.environ["EDGE_TASK_ID"]
state_path = Path(os.environ["EDGE_REAL_STATE"])
fake = fixture.FakeGitHub()
fake.prs[fixture.PR_N] = fixture.make_pr(
    fixture.PR_N,
    state="open",
    merged=False,
    head_sha="contention-open-pr",
)
fake.pr_labels[fixture.PR_N] = []
fake.pr_timeline[fixture.PR_N] = []

real_sync = fixture.mod.sync_board


def record(entry: dict) -> None:
    with state_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def wrapped_sync(board, task_ids=None, *, dry_run=False, client=None):
    results = real_sync(
        board,
        task_ids,
        dry_run=dry_run,
        client=fake,
    )
    row = fixture.task_row(task_id)
    record(
        {
            "role": role,
            "status": row["status"],
            "results": [
                {
                    key: item.get(key)
                    for key in ("task_id", "status", "changed", "reason", "from_state", "to_state")
                    if key in item
                }
                for item in results
            ],
        }
    )
    if role == "owner":
        Path(os.environ["EDGE_OWNER_STARTED"]).write_text("1", encoding="utf-8")
        time.sleep(float(os.environ["EDGE_OWNER_HOLD"]))
    return results


fixture.mod.sync_board = wrapped_sync
fixture.mod.GithubApiClient.from_environment = staticmethod(lambda: fake)
raise SystemExit(fixture.mod._main(["--board", "default", "--json"]))
'''


def _write_script(path: Path, source: str) -> Path:
    path.write_text(f"#!/usr/bin/env python3\n{source}\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _read_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _mark_completion_committed(path: Path) -> None:
    with path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = json.load(handle)
        state["completion_committed"] = True
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _wait_for(path: Path, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {path}")


def _real_intake_body() -> str:
    return (
        "# GitHub Issue intake\n\n"
        "## Provenance\n\n"
        "- source: github-issue\n"
        "- repository: rhgo1749/H4V3-DJ\n"
        "- issue number: 49\n"
        "- issue URL: https://github.com/rhgo1749/H4V3-DJ/issues/49\n"
        "- issue title: contention completion fixture\n"
        "- idempotency key: github:rhgo1749/H4V3-DJ:issue:49\n"
        "- completion contract: github-pr\n\n"
        "## Canonical Issue body\n\n"
        "--- BEGIN GITHUB ISSUE BODY ---\n"
        "Contention completion fixture.\n"
        "--- END GITHUB ISSUE BODY ---\n"
    )


def _create_real_edge_fixture(root: Path) -> str:
    """Create a real Hermes board row that starts in REVIEW."""
    sys.path.insert(0, "/ws/hermes-agent")
    from hermes_cli import kanban_db, kanban_db_connect  # type: ignore

    previous = {
        key: os.environ.get(key)
        for key in (
            "HERMES_HOME",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_INTAKE_HOME",
        )
    }
    os.environ["HERMES_HOME"] = str(root)
    os.environ["HERMES_KANBAN_BOARD"] = "default"
    os.environ.pop("HERMES_KANBAN_DB", None)
    os.environ.pop("HERMES_KANBAN_INTAKE_HOME", None)
    try:
        kanban_db_connect.init_db(board="default")
        with kanban_db_connect.connect_closing(board="default") as conn:
            task_id = kanban_db.create_task(
                conn,
                title="GitHub contention completion fixture",
                body=_real_intake_body(),
                assignee="worker",
                created_by="completion-wake-regression",
                initial_status="running",
                board="default",
            )
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
            conn.commit()
            return task_id
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _commit_real_provisional_done(root: Path, task_id: str) -> None:
    sys.path.insert(0, "/ws/hermes-agent")
    from hermes_cli import kanban_db, kanban_db_connect  # type: ignore

    previous = {
        key: os.environ.get(key)
        for key in (
            "HERMES_HOME",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_INTAKE_HOME",
        )
    }
    os.environ["HERMES_HOME"] = str(root)
    os.environ["HERMES_KANBAN_BOARD"] = "default"
    os.environ.pop("HERMES_KANBAN_DB", None)
    os.environ.pop("HERMES_KANBAN_INTAKE_HOME", None)
    try:
        with kanban_db_connect.connect_closing(board="default") as conn:
            updated = conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? "
                "WHERE id = ? AND status = 'review'",
                (int(time.time()), task_id),
            )
            assert updated.rowcount == 1, task_id
            conn.commit()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _read_real_task(root: Path, task_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sys.path.insert(0, "/ws/hermes-agent")
    from hermes_cli import kanban_db, kanban_db_connect  # type: ignore

    previous = {
        key: os.environ.get(key)
        for key in (
            "HERMES_HOME",
            "HERMES_KANBAN_BOARD",
            "HERMES_KANBAN_DB",
            "HERMES_KANBAN_INTAKE_HOME",
        )
    }
    os.environ["HERMES_HOME"] = str(root)
    os.environ["HERMES_KANBAN_BOARD"] = "default"
    os.environ.pop("HERMES_KANBAN_DB", None)
    os.environ.pop("HERMES_KANBAN_INTAKE_HOME", None)
    try:
        with kanban_db_connect.connect_closing(board="default") as conn:
            row = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
            events = [
                {
                    "kind": event["kind"],
                    "payload": json.loads(event["payload"] or "{}"),
                }
                for event in conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id = ? "
                    "ORDER BY created_at, id",
                    (task_id,),
                ).fetchall()
            ]
            return row, events
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_contention_retry_runs_real_edge_and_parks_done_open_pr() -> None:
    """Require the fresh retry to perform the canonical DONE -> REVIEW write."""
    with tempfile.TemporaryDirectory(prefix="completion-wake-real-edge-") as td:
        root = Path(td)
        state_path = root / "real-state.jsonl"
        attempts_path = root / "attempts.log"
        owner_started = root / "owner-started"
        state_path.touch()
        edge_driver = _write_script(root / "real-edge-driver.py", _REAL_EDGE_DRIVER)
        plugin_driver = _write_script(root / "plugin-driver.py", _PLUGIN_DRIVER)
        task_id = _create_real_edge_fixture(root)

        base_env = os.environ.copy()
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_INTAKE_HOME"):
            base_env.pop(key, None)
        base_env.update(
            {
                "HERMES_HOME": str(root),
                "HERMES_KANBAN_BOARD": "default",
                "GITHUB_TOKEN": "test-token",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HERMES_EDGE_SYNC_TIMEOUT_SECONDS": "0.25",
                "EDGE_HARNESS": str(ROOT / "edge" / "test-kanban-github-sync-rework.py"),
                "EDGE_TASK_ID": task_id,
                "EDGE_REAL_STATE": str(state_path),
                "EDGE_OWNER_STARTED": str(owner_started),
                "EDGE_OWNER_HOLD": "0.36",
                "EDGE_STUB": str(edge_driver),
                "PLUGIN_SOURCE": str(PLUGIN_SOURCE),
                "EDGE_ATTEMPT_LOG": str(attempts_path),
            }
        )

        owner_env = dict(base_env)
        owner_env["EDGE_ROLE"] = "owner"
        owner = subprocess.Popen(
            [sys.executable, str(edge_driver)],
            env=owner_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        completion = None
        try:
            _wait_for(owner_started)
            # Simulate core's already-committed provisional DONE only after
            # the owner completed its REVIEW snapshot under the edge lock.
            _commit_real_provisional_done(root, task_id)

            completion_env = dict(base_env)
            completion_env["EDGE_ROLE"] = "completion"
            completion_env["REAL_COMPLETION_ELIGIBILITY"] = "1"
            completion = subprocess.run(
                [sys.executable, str(plugin_driver)],
                env=completion_env,
                capture_output=True,
                text=True,
                check=False,
                timeout=6.0,
            )
            owner_stdout, owner_stderr = owner.communicate(timeout=6.0)
            assert owner.returncode == 0, (owner_stdout, owner_stderr)
            assert completion.returncode == 0, completion
            assert completion.stderr == "", completion.stderr
            assert json.loads(completion.stdout)["diagnostics"] == [], completion.stdout

            attempts = attempts_path.read_text(encoding="utf-8").splitlines()
            assert attempts == ["attempt", "attempt"], attempts
            records = [
                json.loads(line)
                for line in state_path.read_text(encoding="utf-8").splitlines()
                if line
            ]
            assert [record["role"] for record in records] == ["owner", "completion"], records
            assert records[0]["status"] == "review", records
            completion_result = records[1]["results"]
            assert completion_result == [
                {
                    "task_id": task_id,
                    "status": "review",
                    "changed": True,
                    "reason": "linked_pr_open",
                    "from_state": "done",
                    "to_state": "review",
                }
            ], records

            row, events = _read_real_task(root, task_id)
            assert row["status"] == "review", row
            assert row["completed_at"] is None, row
            sync_events = [event for event in events if event["kind"] == "github_pr_sync"]
            assert len(sync_events) == 1, events
            assert sync_events[0]["payload"]["previous_status"] == "done", sync_events
            assert sync_events[0]["payload"]["new_status"] == "review", sync_events
        finally:
            if owner.poll() is None:
                owner.kill()
            if owner.poll() is None:
                owner.wait(timeout=2.0)


def main() -> int:
    test_contention_retry_runs_real_edge_and_parks_done_open_pr()
    with tempfile.TemporaryDirectory(prefix="completion-wake-retry-") as td:
        root = Path(td)
        state_path = root / "state.json"
        state_path.write_text(
            json.dumps(
                {
                    "completion_committed": False,
                    "events": [],
                    "snapshots": [],
                }
            ),
            encoding="utf-8",
        )
        owner_started = root / "owner-started"
        attempt_marker = root / "completion-attempt-started"
        attempt_log = root / "completion-attempts.log"
        edge_stub = _write_script(root / "edge-stub.py", _EDGE_STUB)
        plugin_driver = _write_script(root / "plugin-driver.py", _PLUGIN_DRIVER)

        base_env = os.environ.copy()
        for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
            base_env.pop(key, None)
        base_env.update(
            {
                "HERMES_HOME": str(root),
                "GITHUB_TOKEN": "test-token",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HERMES_EDGE_SYNC_TIMEOUT_SECONDS": "3.00",
                "EDGE_RETRY_STATE": str(state_path),
                "EDGE_OWNER_STARTED": str(owner_started),
                "EDGE_COMPLETION_ATTEMPT_MARKER": str(attempt_marker),
                "EDGE_COMPLETION_ATTEMPT_LOG": str(attempt_log),
                "EDGE_OWNER_POST_ATTEMPT_HOLD": "3.50",
                "EDGE_COMPLETION_HOLD": "0.05",
                "EDGE_STUB": str(edge_stub),
                "PLUGIN_SOURCE": str(PLUGIN_SOURCE),
            }
        )

        owner_env = dict(base_env)
        owner_env["EDGE_ROLE"] = "owner"
        owner = subprocess.Popen(
            [sys.executable, str(edge_stub), "--board", "default", "--json"],
            env=owner_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for(owner_started)

        # The owner has already recorded its snapshot while the completion is
        # still absent. Commit provisional DONE only after that snapshot.
        _mark_completion_committed(state_path)

        completion_env = dict(base_env)
        completion_env["EDGE_ROLE"] = "completion"
        completion = subprocess.run(
            [sys.executable, str(plugin_driver)],
            env=completion_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
        owner_stdout, owner_stderr = owner.communicate(timeout=10.0)

        assert owner.returncode == 0, (owner_stdout, owner_stderr)
        assert completion.returncode == 0, completion
        assert completion.stderr == "", completion.stderr
        result = json.loads(completion.stdout)
        assert result["diagnostics"] == [], result

        attempts = attempt_log.read_text(encoding="utf-8").splitlines()
        assert attempts == ["attempt", "attempt"], attempts

        state = _read_state(state_path)
        assert state["events"] == [
            "start:owner",
            "end:owner",
            "start:completion",
            "end:completion",
        ], state
        assert state["snapshots"] == [
            {"role": "owner", "completion_committed": False},
            {"role": "completion", "completion_committed": True},
        ], state

    print("completion wake post-contention retry regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
