"""Process regression for completion wake loss behind the canonical edge lock."""
from __future__ import annotations

import fcntl
import importlib.util
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


with lock_path.open("a+b") as lock_handle:
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    update_state(f"start:{role}")
    if role == "owner":
        Path(os.environ["EDGE_OWNER_STARTED"]).write_text("1", encoding="utf-8")
        time.sleep(float(os.environ["EDGE_OWNER_HOLD"]))
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

plugin._completion_is_eligible = lambda task_id, board: True
plugin._edge_script_path = lambda: Path(os.environ["EDGE_STUB"])
plugin._EDGE_TIMEOUT_GRACE_SECONDS = 0.0
diagnostics = []
plugin._diagnostic = lambda task_id, board, code: diagnostics.append(
    {"task_id": task_id, "board": board, "code": code}
)
plugin._on_task_completed(task_id="t_12345678", board="default")
print(json.dumps({"diagnostics": diagnostics}))
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


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {path}")


def main() -> int:
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
                "HERMES_EDGE_SYNC_TIMEOUT_SECONDS": "1.00",
                "EDGE_RETRY_STATE": str(state_path),
                "EDGE_OWNER_STARTED": str(owner_started),
                "EDGE_OWNER_HOLD": "1.50",
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
            timeout=5.0,
        )
        owner_stdout, owner_stderr = owner.communicate(timeout=5.0)

        assert owner.returncode == 0, (owner_stdout, owner_stderr)
        assert completion.returncode == 0, completion
        assert completion.stderr == "", completion.stderr
        result = json.loads(completion.stdout)
        assert result["diagnostics"] == [], result

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