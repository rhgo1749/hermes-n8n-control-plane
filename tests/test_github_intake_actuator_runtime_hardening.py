#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "automation"
    / "hermes"
    / "actuator"
    / "github_intake_actuator.py"
)

spec = importlib.util.spec_from_file_location(
    "github_intake_actuator_runtime_hardening",
    MODULE_PATH,
)
assert spec and spec.loader
actuator: Any = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = actuator
spec.loader.exec_module(actuator)


def test_edge_child_pins_canonical_source_root() -> None:
    original_edge = actuator.EDGE_SYNC_SCRIPT
    original_registry = actuator.REGISTRY_SCRIPT
    original_python = actuator.PYTHON_BIN
    original_token = actuator._github_token
    original_popen = actuator.subprocess.Popen

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        edge = root / "edge.py"
        registry = root / "registry.py"
        edge.write_text("print('[]')\n", encoding="utf-8")
        registry.write_text("# registry\n", encoding="utf-8")

        captured: dict[str, Any] = {}

        def recording_popen(argv, **kwargs):
            captured.update(kwargs)
            return original_popen(argv, **kwargs)

        actuator.EDGE_SYNC_SCRIPT = edge
        actuator.REGISTRY_SCRIPT = registry
        actuator.PYTHON_BIN = Path(sys.executable)
        actuator._github_token = lambda: "test-github-token"
        actuator.subprocess.Popen = recording_popen
        try:
            assert actuator._run_edge_sync("ctrlhangul") == []
            env = captured["env"]
            assert env["PYTHONPATH"] == actuator.HERMES_AGENT_SOURCE_ROOT
            assert env["HERMES_KANBAN_REWORK_DISPATCH"] == "1"
        finally:
            actuator.EDGE_SYNC_SCRIPT = original_edge
            actuator.REGISTRY_SCRIPT = original_registry
            actuator.PYTHON_BIN = original_python
            actuator._github_token = original_token
            actuator.subprocess.Popen = original_popen


def test_edge_failure_diagnostic_is_bounded_and_redacts_token() -> None:
    token = "super-secret-github-token"
    raw = (b"x" * 6000) + token.encode() + b"\nModuleNotFoundError: boom\n"
    diagnostic = actuator._edge_sync_failure_diagnostic(
        raw,
        github_token=token,
    )
    assert token not in diagnostic
    assert "[REDACTED_GITHUB_TOKEN]" in diagnostic
    assert "ModuleNotFoundError: boom" in diagnostic
    # Decoded replacement text may be longer than the raw byte tail only by
    # the redaction marker delta, but the source material is capped first.
    assert len(diagnostic.encode("utf-8")) < (
        actuator.MAX_EDGE_SYNC_DIAGNOSTIC_BYTES + 256
    )


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
