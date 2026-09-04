#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (
    ROOT
    / "automation"
    / "hermes"
    / "scripts"
    / "github-agent-ready-kanban-intake-entrypoint.py"
)

spec = importlib.util.spec_from_file_location(
    "github_intake_full_fallback_entrypoint_test",
    ENTRYPOINT,
)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def _fake_module(scope, snapshot):
    fake = ModuleType("fake_full_fallback_intake")
    fake.WakeScope = mod._core.WakeScope
    fake.IntakeError = mod._core.IntakeError
    fake._github_token = lambda: "token"
    fake._load_registry_snapshot = lambda token: snapshot
    fake._claim_wake_scope = lambda: scope
    fake._requeue_wake_scope = lambda claimed, reason: None
    return fake


def test_full_scope_expands_all_registry_repositories_into_event_onboarding_scope():
    original = mod._core.WakeScope(
        mode="full",
        repositories=(),
        expires_at=9999999999,
        scope_id="scope-1",
        claim_token="a" * 32,
    )
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {"repository": "rhgo1749/re-bound", "ready": False},
            {"repository": "rhgo1749/H4V3-DJ", "ready": True},
            {"repository": "rhgo1749/re-bound", "ready": False},
        ],
    }
    fake = _fake_module(original, snapshot)

    mod._install_full_scope_onboarding_overlay(fake)
    expanded = fake._claim_wake_scope()

    assert expanded.mode == "event"
    assert expanded.repositories == (
        "rhgo1749/H4V3-DJ",
        "rhgo1749/re-bound",
    )
    assert expanded.scope_id == original.scope_id
    assert expanded.claim_token == original.claim_token
    assert expanded.expires_at == original.expires_at


def test_event_scope_is_not_rewritten_or_forced_through_registry():
    original = mod._core.WakeScope(
        mode="event",
        repositories=("rhgo1749/ctrl-hangul",),
        expires_at=9999999999,
        scope_id="scope-2",
        claim_token="b" * 32,
    )
    fake = _fake_module(original, {"repositories": []})
    fake._load_registry_snapshot = lambda token: (_ for _ in ()).throw(
        AssertionError("event scope must not scan registry in the overlay")
    )

    mod._install_full_scope_onboarding_overlay(fake)

    assert fake._claim_wake_scope() is original


def test_registry_failure_requeues_already_claimed_full_scope_before_failing():
    original = mod._core.WakeScope(
        mode="full",
        repositories=(),
        expires_at=9999999999,
        scope_id="scope-3",
        claim_token="c" * 32,
    )
    fake = _fake_module(original, {"repositories": []})
    transitions: list[tuple[str, str]] = []
    fake._load_registry_snapshot = lambda token: (_ for _ in ()).throw(
        fake.IntakeError("registry_unavailable")
    )
    fake._requeue_wake_scope = lambda scope, reason: transitions.append(
        (scope.scope_id, reason)
    )

    mod._install_full_scope_onboarding_overlay(fake)

    with pytest.raises(fake.IntakeError, match="registry_unavailable"):
        fake._claim_wake_scope()

    assert transitions == [("scope-3", "registry_unavailable")]


def test_empty_registry_keeps_full_scope_semantics():
    original = mod._core.WakeScope(
        mode="full",
        repositories=(),
        expires_at=9999999999,
        scope_id="scope-4",
        claim_token="d" * 32,
    )
    fake = _fake_module(original, {"schema_version": 2, "repositories": []})

    mod._install_full_scope_onboarding_overlay(fake)

    assert fake._claim_wake_scope() is original
