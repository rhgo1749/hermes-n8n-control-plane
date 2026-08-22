"""Executable process-level regressions for the edge single-flight boundary."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EDGE_CORE = ROOT / "edge" / "kanban-github-sync.py"
ACTUATOR_SOURCE = ROOT / "automation" / "hermes" / "actuator" / "github_intake_actuator.py"
PLUGIN_SOURCE = ROOT / "hermes-plugin" / "github-completion-edge-wake" / "__init__.py"

_TARGET_SOURCE = r'''
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

source = Path(os.environ["EDGE_CORE"])
spec = importlib.util.spec_from_file_location("edge_single_flight_target", source)
assert spec is not None and spec.loader is not None
edge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = edge
spec.loader.exec_module(edge)


def update_state(delta: int, event: str) -> None:
    path = Path(os.environ["EDGE_STATE"])
    with path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = json.load(handle)
        state["active"] += delta
        state["max_active"] = max(state["max_active"], state["active"])
        state["events"].append(event)
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def fake_sync(board: str, task_ids=None, *, dry_run=False, client=None):
    run = os.environ["EDGE_RUN"]
    update_state(1, f"start:{run}")
    try:
        with Path(os.environ["EDGE_NOTIFY_PATH"]).open("w", encoding="utf-8") as signal:
            signal.write("started\n")
            signal.flush()
        if os.environ.get("EDGE_CRASH_AFTER_START") == "1":
            os._exit(17)
        time.sleep(float(os.environ["EDGE_HOLD"]))
    finally:
        update_state(-1, f"end:{run}")
    return [{"task_id": run, "status": "done", "changed": False}]


edge.sync_board = fake_sync
raise SystemExit(edge._main(["--board", "default", "--json"]))
'''

_ACTUATOR_SOURCE = r'''
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

source = Path(os.environ["ACTUATOR_SOURCE"])
spec = importlib.util.spec_from_file_location("single_flight_actuator", source)
assert spec is not None and spec.loader is not None
actuator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = actuator
spec.loader.exec_module(actuator)
actuator.PYTHON_BIN = Path(sys.executable)
actuator.EDGE_SYNC_SCRIPT = Path(os.environ["EDGE_TARGET"])
actuator.REGISTRY_SCRIPT = Path(os.environ["REGISTRY"])
actuator._github_token = lambda: "test-token"
print(json.dumps({"results": actuator._run_edge_sync("default")}))
'''

_PLUGIN_SOURCE = r'''
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

source = Path(os.environ["PLUGIN_SOURCE"])
spec = importlib.util.spec_from_file_location("single_flight_plugin", source)
assert spec is not None and spec.loader is not None
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
plugin._completion_is_eligible = lambda task_id, board: True
plugin._edge_script_path = lambda: Path(os.environ["EDGE_TARGET"])
plugin._EDGE_TIMEOUT_SECONDS = float(os.environ.get("EDGE_PLUGIN_TIMEOUT", "5"))
diagnostics = []
plugin._diagnostic = lambda task_id, board, code: diagnostics.append({
    "task_id": task_id,
    "board": board,
    "code": code,
})
plugin._on_task_completed(task_id="t_12345678", board="default")
print(json.dumps({"diagnostics": diagnostics}))
'''


def _write_script(path: Path, source: str) -> Path:
    path.write_text(f"#!/usr/bin/env python3\n{source}\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _initialise_state(path: Path) -> None:
    path.write_text(
        json.dumps({"active": 0, "max_active": 0, "events": []}),
        encoding="utf-8",
    )


def _environment(
    root: Path,
    state: Path,
    target: Path,
    registry: Path,
    run: str,
    hold: float,
    *,
    notify_path: Path,
    script: str,
    plugin_timeout: float | None = None,
    crash_after_start: bool = False,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_EDGE_SYNC_TIMEOUT_SECONDS",
    ):
        env.pop(key, None)
    env.update(
        {
            "HERMES_HOME": str(root),
            "HERMES_KANBAN_INTAKE_HOME": str(root),
            "GITHUB_TOKEN": "test-token",
            "PYTHONDONTWRITEBYTECODE": "1",
            "EDGE_CORE": str(EDGE_CORE),
            "EDGE_TARGET": str(target),
            "EDGE_STATE": str(state),
            "EDGE_RUN": run,
            "EDGE_HOLD": str(hold),
            "EDGE_NOTIFY_PATH": str(notify_path),
            "REGISTRY": str(registry),
            "ACTUATOR_SOURCE": str(ACTUATOR_SOURCE),
            "PLUGIN_SOURCE": str(PLUGIN_SOURCE),
        }
    )
    if plugin_timeout is not None:
        env["EDGE_PLUGIN_TIMEOUT"] = str(plugin_timeout)
    else:
        env.pop("EDGE_PLUGIN_TIMEOUT", None)
    if crash_after_start:
        env["EDGE_CRASH_AFTER_START"] = "1"
    else:
        env.pop("EDGE_CRASH_AFTER_START", None)
    env["EDGE_DRIVER_SCRIPT"] = script
    return env


def _start(
    *,
    root: Path,
    state: Path,
    target: Path,
    registry: Path,
    run: str,
    hold: float,
    driver: Path,
    plugin_timeout: float | None = None,
    crash_after_start: bool = False,
) -> tuple[subprocess.Popen[str], int]:
    notify_path = root / f"{run}.fifo"
    os.mkfifo(notify_path, 0o600)
    read_fd = os.open(notify_path, os.O_RDONLY | os.O_NONBLOCK)
    env = _environment(
        root,
        state,
        target,
        registry,
        run,
        hold,
        notify_path=notify_path,
        script=str(driver),
        plugin_timeout=plugin_timeout,
        crash_after_start=crash_after_start,
    )
    process = subprocess.Popen(
        [sys.executable, str(driver)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, read_fd


def _wait_started(process: subprocess.Popen[str], read_fd: int) -> None:
    try:
        ready, _, _ = select.select([read_fd], [], [], 5.0)
        if not ready:
            stdout, stderr = process.communicate(timeout=1.0)
            raise AssertionError(
                f"edge child did not acquire lock: rc={process.returncode} "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        assert os.read(read_fd, 64).startswith(b"started")
    finally:
        os.close(read_fd)


def _finish(process: subprocess.Popen[str]) -> tuple[str, str]:
    stdout, stderr = process.communicate(timeout=8.0)
    assert process.returncode == 0, (
        f"child failed: rc={process.returncode} stdout={stdout!r} stderr={stderr!r}"
    )
    return stdout, stderr


def _read_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixtures(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    state = root / "state.json"
    target = _write_script(root / "edge-target.py", _TARGET_SOURCE)
    actuator = _write_script(root / "actuator-driver.py", _ACTUATOR_SOURCE)
    plugin = _write_script(root / "plugin-driver.py", _PLUGIN_SOURCE)
    registry = root / "repository_registry.py"
    registry.write_text("# fixture\n", encoding="utf-8")
    _initialise_state(state)
    return state, target, actuator, plugin, registry


def test_webhook_and_completion_paths_are_serialized() -> None:
    with tempfile.TemporaryDirectory(prefix="edge-single-flight-") as td:
        root = Path(td)
        state, target, actuator, plugin, registry = _fixtures(root)
        first, first_signal = _start(
            root=root,
            state=state,
            target=target,
            registry=registry,
            run="actuator",
            hold=0.30,
            driver=actuator,
        )
        _wait_started(first, first_signal)
        second, second_signal = _start(
            root=root,
            state=state,
            target=target,
            registry=registry,
            run="completion",
            hold=0.05,
            driver=plugin,
        )
        try:
            _finish(second)
            _finish(first)
        finally:
            os.close(second_signal)
        state_value = _read_state(state)
        assert state_value["active"] == 0, state_value
        assert state_value["max_active"] == 1, state_value
        assert state_value["events"] == [
            "start:actuator",
            "end:actuator",
            "start:completion",
            "end:completion",
        ], state_value


def test_two_completion_wakes_are_serialized_and_both_run() -> None:
    with tempfile.TemporaryDirectory(prefix="edge-single-flight-") as td:
        root = Path(td)
        state, target, _actuator, plugin, registry = _fixtures(root)
        first, first_signal = _start(
            root=root,
            state=state,
            target=target,
            registry=registry,
            run="completion-a",
            hold=0.22,
            driver=plugin,
        )
        _wait_started(first, first_signal)
        second, second_signal = _start(
            root=root,
            state=state,
            target=target,
            registry=registry,
            run="completion-b",
            hold=0.05,
            driver=plugin,
        )
        try:
            _finish(second)
            _finish(first)
        finally:
            os.close(second_signal)
        state_value = _read_state(state)
        assert state_value["active"] == 0, state_value
        assert state_value["max_active"] == 1, state_value
        assert state_value["events"] == [
            "start:completion-a",
            "end:completion-a",
            "start:completion-b",
            "end:completion-b",
        ], state_value


def test_contention_timeout_is_an_explicit_failed_wake() -> None:
    with tempfile.TemporaryDirectory(prefix="edge-single-flight-") as td:
        root = Path(td)
        state, target, _actuator, plugin, registry = _fixtures(root)
        owner, owner_signal = _start(
            root=root,
            state=state,
            target=target,
            registry=registry,
            run="owner",
            hold=0.35,
            driver=plugin,
        )
        _wait_started(owner, owner_signal)
        waiter, waiter_signal = _start(
            root=root,
            state=state,
            target=target,
            registry=registry,
            run="timed-out-wake",
            hold=0.05,
            driver=plugin,
            plugin_timeout=0.05,
        )
        os.close(waiter_signal)
        waiter_stdout, waiter_stderr = _finish(waiter)
        _finish(owner)
        assert waiter_stderr == "", waiter_stderr
        waiter_result = json.loads(waiter_stdout)
        assert waiter_result["diagnostics"] == [
            {"task_id": "t_12345678", "board": "default", "code": "edge_timeout"}
        ], waiter_result
        state_value = _read_state(state)
        assert state_value["active"] == 0, state_value
        assert state_value["max_active"] == 1, state_value
        assert state_value["events"] == ["start:owner", "end:owner"], state_value


def test_crashed_owner_releases_kernel_lock() -> None:
    with tempfile.TemporaryDirectory(prefix="edge-single-flight-") as td:
        root = Path(td)
        state, target, _actuator, plugin, registry = _fixtures(root)
        crashed, crashed_signal = _start(
            root=root,
            state=state,
            target=target,
            registry=registry,
            run="crashed-owner",
            hold=0.30,
            driver=plugin,
            crash_after_start=True,
        )
        _wait_started(crashed, crashed_signal)
        recovered, recovered_signal = _start(
            root=root,
            state=state,
            target=target,
            registry=registry,
            run="recovered-wake",
            hold=0.05,
            driver=plugin,
        )
        _wait_started(recovered, recovered_signal)
        _finish(recovered)
        crashed_stdout, crashed_stderr = crashed.communicate(timeout=5.0)
        assert crashed.returncode == 0, crashed_stderr
        assert json.loads(crashed_stdout)["diagnostics"] == [
            {"task_id": "t_12345678", "board": "default", "code": "edge_nonzero"}
        ], crashed_stdout
        state_value = _read_state(state)
        assert state_value["events"] == [
            "start:crashed-owner",
            "start:recovered-wake",
            "end:recovered-wake",
        ], state_value


def test_symlinked_runtime_lock_root_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="edge-single-flight-") as td:
        root = Path(td)
        state, target, _actuator, _plugin, _registry = _fixtures(root)
        real_kanban = root / "real-kanban"
        real_kanban.mkdir()
        (root / "kanban").symlink_to(real_kanban, target_is_directory=True)
        env = os.environ.copy()
        env.update(
            {
                "HERMES_HOME": str(root),
                "HERMES_KANBAN_INTAKE_HOME": str(root),
                "GITHUB_TOKEN": "test-token",
                "PYTHONDONTWRITEBYTECODE": "1",
                "EDGE_CORE": str(EDGE_CORE),
                "EDGE_STATE": str(state),
                "EDGE_RUN": "symlink",
                "EDGE_HOLD": "0",
                "EDGE_NOTIFY_PATH": str(root / "unused.fifo"),
            }
        )
        completed = subprocess.run(
            [sys.executable, str(target), "--board", "default", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 1, completed
        assert "edge_single_flight_path_invalid" in completed.stderr, completed
        assert _read_state(state)["events"] == [], state


def main() -> int:
    test_webhook_and_completion_paths_are_serialized()
    test_two_completion_wakes_are_serialized_and_both_run()
    test_contention_timeout_is_an_explicit_failed_wake()
    test_crashed_owner_releases_kernel_lock()
    test_symlinked_runtime_lock_root_fails_closed()
    print("edge single-flight process concurrency regressions: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
