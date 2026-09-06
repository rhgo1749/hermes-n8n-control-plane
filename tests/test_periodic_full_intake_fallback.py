#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "automation" / "n8n" / "github-router" / "router_entrypoint.py"
spec = importlib.util.spec_from_file_location("github_router_periodic_entrypoint", MODULE_PATH)
assert spec and spec.loader
entry: Any = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = entry
spec.loader.exec_module(entry)


class FakeCore:
    def __init__(self, *, scope: dict[str, Any] | None = None) -> None:
        self.scope = scope or {
            "id": "scope-periodic",
            "mode": "full",
            "repositories": [],
        }
        self.enqueue_calls: list[dict[str, Any]] = []
        self.wake_calls = 0

    def _enqueue_scope(self, **kwargs: Any) -> dict[str, Any]:
        self.enqueue_calls.append(kwargs)
        return dict(self.scope)

    def _wake(self) -> dict[str, Any]:
        self.wake_calls += 1
        return {"lease": "lease-periodic", "upstream_status": 200}


def test_periodic_full_intake_uses_stable_full_scope_identity() -> None:
    core = FakeCore()

    result = entry._periodic_full_intake_once(core)

    assert core.enqueue_calls == [
        {
            "full": True,
            "delivery_id": "router-periodic-full-intake",
        }
    ]
    assert core.wake_calls == 1
    assert result["scope"]["mode"] == "full"
    assert result["wake"]["upstream_status"] == 200


def test_existing_queued_periodic_scope_is_rewoken_not_duplicated() -> None:
    core = FakeCore(
        scope={
            "id": "scope-periodic",
            "mode": "full",
            "repositories": [],
            "existing": True,
        }
    )

    result = entry._periodic_full_intake_once(core)

    assert len(core.enqueue_calls) == 1
    assert core.wake_calls == 1
    assert result["scope"]["existing"] is True


@pytest.mark.parametrize("active_flag", ["in_flight", "pending"])
def test_active_or_pending_periodic_scope_does_not_double_wake(active_flag: str) -> None:
    core = FakeCore(
        scope={
            "id": "scope-periodic",
            "mode": "full",
            "repositories": [],
            "existing": True,
            active_flag: True,
        }
    )

    result = entry._periodic_full_intake_once(core)

    assert core.wake_calls == 0
    assert result["wake"] == {
        "skipped": True,
        "reason": "scope_already_active_or_pending",
    }


def test_periodic_loop_waits_before_first_safety_wake() -> None:
    core = FakeCore()

    class StopImmediately:
        def __init__(self) -> None:
            self.waits: list[int] = []

        def wait(self, timeout: int) -> bool:
            self.waits.append(timeout)
            return True

    stop = StopImmediately()
    entry._periodic_full_intake_loop(core, 3600, stop)

    assert stop.waits == [3600]
    assert core.enqueue_calls == []
    assert core.wake_calls == 0


def test_fallback_interval_defaults_to_hourly_and_rejects_high_frequency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS", raising=False)
    assert entry._fallback_interval_seconds() == 3600

    monkeypatch.setenv("GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS", "299")
    with pytest.raises(RuntimeError, match="at least 300"):
        entry._fallback_interval_seconds()

    monkeypatch.setenv("GITHUB_ROUTER_FALLBACK_INTERVAL_SECONDS", "900")
    assert entry._fallback_interval_seconds() == 900
