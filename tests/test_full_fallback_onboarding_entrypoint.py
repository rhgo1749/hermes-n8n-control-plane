#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
    fake._run_once = lambda args: 0
    fake._provision_scoped_checkouts = (
        lambda token, repositories, snapshot, *, dry_run: ([], [], False)
    )
    return fake


def _full_scope(scope_id: str = "scope-1"):
    return mod._core.WakeScope(
        mode="full",
        repositories=(),
        expires_at=9999999999,
        scope_id=scope_id,
        claim_token="a" * 32,
    )


def test_full_scope_onboards_only_missing_checkout_and_stays_full():
    original = _full_scope()
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {
                "repository": "rhgo1749/ctrl-hangul",
                "ready": True,
                "checkout_status": "verified",
            },
            {
                "repository": "rhgo1749/new-agent",
                "ready": False,
                "checkout_status": "missing",
                "reason": "checkout_missing",
            },
            {
                "repository": "rhgo1749/re-bound",
                "ready": True,
                "checkout_status": "verified",
            },
        ],
    }
    calls = []
    fake = _fake_module(original, snapshot)

    def provision(token, repositories, supplied_snapshot, *, dry_run):
        calls.append((token, tuple(repositories), supplied_snapshot, dry_run))
        return (
            [
                {
                    "repository": "rhgo1749/new-agent",
                    "checkout": "/ws/projects/new-agent",
                    "action": "registered",
                }
            ],
            [],
            True,
        )

    fake._provision_scoped_checkouts = provision
    mod._install_full_scope_onboarding_overlay(fake)

    claimed = fake._claim_wake_scope()

    assert claimed is original
    assert claimed.mode == "full"
    assert calls == [
        (
            "token",
            ("rhgo1749/new-agent",),
            snapshot,
            False,
        )
    ]


def test_full_scope_does_not_strictly_revalidate_ready_checkouts():
    original = _full_scope("scope-ready")
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {
                "repository": "rhgo1749/ctrl-hangul",
                "ready": True,
                "checkout_status": "verified",
            },
            {
                "repository": "rhgo1749/re-bound",
                "ready": True,
                "checkout_status": "verified",
            },
        ],
    }
    fake = _fake_module(original, snapshot)
    fake._provision_scoped_checkouts = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("ready repositories must remain on historical full-scan path")
    )

    mod._install_full_scope_onboarding_overlay(fake)

    assert fake._claim_wake_scope() is original


def test_missing_checkout_skip_does_not_poison_existing_full_sweep():
    original = _full_scope("scope-skip")
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {
                "repository": "rhgo1749/new-agent",
                "ready": False,
                "checkout_status": "missing",
                "reason": "checkout_missing",
            },
            {
                "repository": "rhgo1749/ctrl-hangul",
                "ready": True,
                "checkout_status": "verified",
            },
        ],
    }
    transitions = []
    fake = _fake_module(original, snapshot)
    fake._requeue_wake_scope = lambda claimed, reason: transitions.append(
        (claimed.scope_id, reason)
    )
    fake._provision_scoped_checkouts = (
        lambda token, repositories, supplied_snapshot, *, dry_run: (
            [],
            [
                {
                    "repository": "rhgo1749/new-agent",
                    "reason": "checkout_path_conflict",
                }
            ],
            False,
        )
    )

    mod._install_full_scope_onboarding_overlay(fake)

    assert fake._claim_wake_scope() is original
    assert transitions == []


def test_full_scope_dry_run_is_forwarded_to_missing_checkout_provisioning():
    original = _full_scope("scope-dry")
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {
                "repository": "rhgo1749/new-agent",
                "ready": False,
                "checkout_status": "missing",
                "reason": "checkout_missing",
            }
        ],
    }
    calls = []
    fake = _fake_module(original, snapshot)
    fake._provision_scoped_checkouts = (
        lambda token, repositories, supplied_snapshot, *, dry_run: (
            calls.append(dry_run) or [],
            [],
            False,
        )
    )

    mod._install_full_scope_onboarding_overlay(fake)
    mod._install_full_scope_dry_run_overlay(fake)

    assert fake._run_once(argparse.Namespace(dry_run=True)) == 0
    assert fake.__dict__.get("_full_scope_onboarding_dry_run") is None

    fake.__dict__["_full_scope_onboarding_dry_run"] = True
    try:
        assert fake._claim_wake_scope() is original
    finally:
        fake.__dict__.pop("_full_scope_onboarding_dry_run", None)

    assert calls == [True]


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
    original = _full_scope("scope-3")
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
    original = _full_scope("scope-4")
    fake = _fake_module(original, {"schema_version": 2, "repositories": []})

    mod._install_full_scope_onboarding_overlay(fake)

    assert fake._claim_wake_scope() is original
