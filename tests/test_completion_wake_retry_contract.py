"""Focused unit contract for the completion wake's single fresh retry."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hermes-plugin" / "github-completion-edge-wake" / "__init__.py"
spec = importlib.util.spec_from_file_location("completion_wake_retry_contract", SOURCE)
assert spec is not None and spec.loader is not None
mod: Any = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def main() -> int:
    original_run_edge = mod._run_edge
    original_eligible = mod._completion_is_eligible
    edge_path = Path("/tmp/fixed-edge.py")
    try:
        calls: list[str] = []

        def timed_out_then_success(path: Path, board: str):
            calls.append(board)
            if len(calls) == 1:
                return mod.WakeResult(returncode=-9, output_bytes=0, timed_out=True)
            return mod.WakeResult(returncode=0, output_bytes=2)

        mod._run_edge = timed_out_then_success
        mod._completion_is_eligible = lambda task_id, board: True
        result = mod._run_edge_with_contention_retry(
            edge_path, "default", "t_12345678"
        )
        assert result.returncode == 0 and not result.timed_out, result
        assert calls == ["default", "default"], calls

        calls.clear()
        mod._completion_is_eligible = lambda task_id, board: False
        result = mod._run_edge_with_contention_retry(
            edge_path, "default", "t_12345678"
        )
        assert result.returncode == 0 and not result.timed_out, result
        assert calls == ["default"], calls

        calls.clear()

        def uncertain_eligibility(task_id: str, board: str) -> bool:
            raise mod._WakeFailure("board_read_failed")

        mod._completion_is_eligible = uncertain_eligibility
        result = mod._run_edge_with_contention_retry(
            edge_path, "default", "t_12345678"
        )
        assert result.returncode == 0 and not result.timed_out, result
        assert calls == ["default", "default"], calls

        calls.clear()

        def non_timeout_failure(path: Path, board: str):
            calls.append(board)
            return mod.WakeResult(returncode=3, output_bytes=0)

        mod._run_edge = non_timeout_failure
        mod._completion_is_eligible = lambda task_id, board: True
        result = mod._run_edge_with_contention_retry(
            edge_path, "default", "t_12345678"
        )
        assert result.returncode == 3 and not result.timed_out, result
        assert calls == ["default"], calls
    finally:
        mod._run_edge = original_run_edge
        mod._completion_is_eligible = original_eligible

    print("completion wake bounded retry contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())