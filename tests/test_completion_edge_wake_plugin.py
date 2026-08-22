"""Executable regression checks for the completion-side edge wake runner."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, "/ws/hermes-agent")

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hermes-plugin" / "github-completion-edge-wake" / "__init__.py"
spec = importlib.util.spec_from_file_location("completion_edge_wake_test", SOURCE)
assert spec is not None and spec.loader is not None
mod: Any = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def script(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def main() -> int:
    from hermes_cli import plugins as plugin_api  # type: ignore

    previous_home = os.environ.get("HERMES_HOME")
    with tempfile.TemporaryDirectory(prefix="completion-edge-wake-plugin-") as td:
        home = Path(td)
        installed = home / "plugins" / "github-completion-edge-wake"
        installed.mkdir(parents=True)
        shutil.copy2(SOURCE, installed / "__init__.py")
        shutil.copy2(SOURCE.with_name("plugin.yaml"), installed / "plugin.yaml")
        shutil.copy2(
            ROOT / "automation" / "hermes" / "edge_sync_timeout.py",
            installed / "edge_sync_timeout.py",
        )
        (home / "config.yaml").write_text(
            "plugins:\n  enabled:\n    - github-completion-edge-wake\n",
            encoding="utf-8",
        )
        os.environ["HERMES_HOME"] = str(home)
        plugin_api._reset_plugin_managers_for_tests()
        try:
            plugin_api.discover_plugins(force=True)
            manager = plugin_api.get_plugin_manager()
            loaded = manager._plugins["github-completion-edge-wake"]
            assert loaded.enabled, loaded
            assert len(manager._hooks["kanban_task_completed"]) == 1
        finally:
            plugin_api._reset_plugin_managers_for_tests()
            if previous_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous_home

    with tempfile.TemporaryDirectory(prefix="completion-edge-wake-") as td:
        root = Path(td)
        env_keys = (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "HERMES_GITHUB_TOKEN",
            "HERMES_HOME",
            "HERMES_EDGE_SYNC_TIMEOUT_SECONDS",
            "EDGE_TOKEN_MARKER",
        )
        previous_env = {key: os.environ.get(key) for key in env_keys}
        os.environ["GITHUB_TOKEN"] = "environment-token"
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("HERMES_GITHUB_TOKEN", None)
        try:
            success = script(
                root / "kanban-github-sync.py",
                "import sys; assert sys.argv[1:] == ['--board', 'default', '--json']; print('ok')",
            )
            result = mod._run_edge(success, "default")
            assert result.returncode == 0, result
            assert result.output_bytes > 0, result
            assert not result.timed_out and not result.output_limited, result

            precedence = script(
                root / "precedence.py",
                "import os; assert os.environ['GITHUB_TOKEN'] == 'environment-token'; print(os.environ['GITHUB_TOKEN'])",
            )
            result = mod._run_edge(precedence, "default")
            assert result.returncode == 0, result

            failure = script(root / "failure.py", "raise SystemExit(3)")
            result = mod._run_edge(failure, "default")
            assert result.returncode == 3, result

            noisy = script(
                root / "noisy.py",
                "import sys; sys.stdout.write('x' * 1000000); sys.stdout.flush()",
            )
            result = mod._run_edge(noisy, "default")
            assert result.output_limited, result
            assert result.output_bytes > mod._EDGE_OUTPUT_LIMIT_BYTES, result

            slow = script(root / "slow.py", "import time; time.sleep(6)")
            original_timeout = os.environ.get("HERMES_EDGE_SYNC_TIMEOUT_SECONDS")
            os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = "0.05"
            try:
                result = mod._run_edge(slow, "default")
            finally:
                if original_timeout is None:
                    os.environ.pop("HERMES_EDGE_SYNC_TIMEOUT_SECONDS", None)
                else:
                    os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = original_timeout
            assert result.timed_out, result

            assert mod._edge_timeout_seconds() > 120.0
            os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = "0.05"
            assert mod._edge_timeout_seconds() == 0.05 + mod._EDGE_TIMEOUT_GRACE_SECONDS
            bounded_slow = script(
                root / "bounded-slow.py", "import time; time.sleep(0.1)"
            )
            result = mod._run_edge(bounded_slow, "default")
            assert not result.timed_out, result

            os.environ["HERMES_EDGE_SYNC_TIMEOUT_SECONDS"] = "inf"
            try:
                mod._edge_timeout_seconds()
            except mod._WakeFailure as exc:
                assert exc.code == "edge_timeout_invalid", exc.code
            else:
                raise AssertionError("non-finite edge timeout must fail closed")
            os.environ.pop("HERMES_EDGE_SYNC_TIMEOUT_SECONDS", None)

            fallback_token = "fallback-token-value"
            fallback_home = root / "hermes-home"
            fallback_home.mkdir()
            (fallback_home / ".env").write_text(
                f"GITHUB_TOKEN={fallback_token}\n", encoding="utf-8"
            )
            marker = root / "token-marker"
            os.environ.pop("GITHUB_TOKEN", None)
            os.environ["HERMES_HOME"] = str(fallback_home)
            os.environ["EDGE_TOKEN_MARKER"] = str(marker)
            fallback = script(
                root / "fallback.py",
                f"""
import os
from pathlib import Path
token = os.environ.get("GITHUB_TOKEN")
assert token == {fallback_token!r}
Path(os.environ["EDGE_TOKEN_MARKER"]).write_text("received", encoding="utf-8")
print(token)
""",
            )
            result = mod._run_edge(fallback, "default")
            assert result.returncode == 0, result
            assert marker.read_text(encoding="utf-8") == "received"
            assert fallback_token not in repr(result)
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    legacy_body = (
        "- source: github-issue\n"
        "- repository: rhgo1749/hermes-n8n-control-plane\n"
        "\n## Canonical Issue body\n"
        "- completion contract: github-pr\n"
    )
    assert mod._is_github_backed_body(legacy_body), legacy_body
    assert not mod._is_github_backed_body(
        "## Canonical Issue body\n- source: github-issue\n"
    )

    try:
        mod._valid_board("../escape")
    except mod._WakeFailure as exc:
        assert exc.code == "invalid_board", exc.code
    else:
        raise AssertionError("invalid board must fail closed")

    print("completion edge wake plugin discovery + runner checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
