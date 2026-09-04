#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def _full_scope(scope_id: str = "scope-1"):
    return mod._core.WakeScope(
        mode="full",
        repositories=(),
        expires_at=9999999999,
        scope_id=scope_id,
        claim_token="a" * 32,
    )


def _event_scope(repository: str = "rhgo1749/ctrl-hangul"):
    return mod._core.WakeScope(
        mode="event",
        repositories=(repository,),
        expires_at=9999999999,
        scope_id="scope-event",
        claim_token="b" * 32,
    )


def _metadata(repository: str, sha: str = "a" * 40):
    return SimpleNamespace(
        repository=repository,
        default_branch="main",
        default_branch_sha=sha,
        contract_paths=("AGENTS.md",),
    )


def _fake_module(scope, snapshot):
    fake = ModuleType("fake_full_fallback_intake")
    fake.WakeScope = mod._core.WakeScope
    fake.IntakeError = mod._core.IntakeError
    fake.RepoSnapshot = mod._core.RepoSnapshot
    fake.ONBOARDING_CONTRACT_CANDIDATES = mod._core.ONBOARDING_CONTRACT_CANDIDATES
    fake._ONBOARDING_SHA = mod._core._ONBOARDING_SHA
    fake._github_token = lambda: "token"
    fake._load_registry_snapshot = lambda token: snapshot
    fake._claim_wake_scope = lambda: scope
    fake._requeue_wake_scope = lambda claimed, reason: None
    fake._run_once = lambda args: 0
    fake._provision_scoped_checkouts = (
        lambda token, repositories, supplied_snapshot, *, dry_run: ([], [], False)
    )
    fake._onboarding_repository_metadata = (
        lambda token, repository: _metadata(repository)
    )
    fake._onboarding_error_code = lambda exc: str(exc).split(":", 1)[0]
    fake._active_scope_progress = {}
    return fake


def test_full_scope_onboards_only_missing_checkout_and_stays_full():
    original = _full_scope()
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {
                "repository": "rhgo1749/ctrl-hangul",
                "ready": True,
                "checkout_status": "verified",
                "checkout": "/ws/projects/ctrl-hangul",
                "default_branch": "main",
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
                "checkout": "/ws/projects/re-bound",
                "default_branch": "main",
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
                "checkout": "/ws/projects/ctrl-hangul",
                "default_branch": "main",
            },
            {
                "repository": "rhgo1749/re-bound",
                "ready": True,
                "checkout_status": "verified",
                "checkout": "/ws/projects/re-bound",
                "default_branch": "main",
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
                "checkout": "/ws/projects/ctrl-hangul",
                "default_branch": "main",
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


def test_event_scope_is_not_rewritten_by_full_scope_overlay():
    original = _event_scope()
    fake = _fake_module(original, {"repositories": []})
    fake._load_registry_snapshot = lambda token: (_ for _ in ()).throw(
        AssertionError("event scope must not scan registry in full-scope overlay")
    )

    mod._install_full_scope_onboarding_overlay(fake)

    assert fake._claim_wake_scope() is original


def test_event_ready_repository_reuses_registry_checkout_without_strict_head_gate():
    repository = "rhgo1749/ctrl-hangul"
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {
                "repository": repository,
                "ready": True,
                "checkout_status": "verified",
                "checkout": "/ws/projects/ctrl-hangul",
                "default_branch": "main",
            }
        ],
    }
    fake = _fake_module(_event_scope(repository), snapshot)
    strict_calls = []
    fake._provision_scoped_checkouts = (
        lambda token, repositories, supplied_snapshot, *, dry_run: (
            strict_calls.append(tuple(repositories)) or [],
            [],
            False,
        )
    )

    mod._install_existing_ready_scope_overlay(fake)

    results, skipped, reload_required = fake._provision_scoped_checkouts(
        "token",
        (repository,),
        {},
        dry_run=False,
    )

    assert strict_calls == []
    assert results == [
        {
            "repository": repository,
            "checkout": "/ws/projects/ctrl-hangul",
            "action": "reused",
        }
    ]
    assert skipped == []
    assert reload_required is False
    assert fake._active_scope_progress["checkout_provisioning"] == results


def test_event_missing_repository_still_uses_strict_onboarding():
    repository = "rhgo1749/new-agent"
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {
                "repository": repository,
                "ready": False,
                "checkout_status": "missing",
                "reason": "checkout_missing",
            }
        ],
    }
    fake = _fake_module(_event_scope(repository), snapshot)
    strict_calls = []

    def strict(token, repositories, supplied_snapshot, *, dry_run):
        strict_calls.append((tuple(repositories), dry_run))
        return (
            [
                {
                    "repository": repository,
                    "checkout": "/ws/projects/new-agent",
                    "action": "registered",
                }
            ],
            [],
            True,
        )

    fake._provision_scoped_checkouts = strict
    mod._install_existing_ready_scope_overlay(fake)

    results, skipped, reload_required = fake._provision_scoped_checkouts(
        "token",
        (repository,),
        {},
        dry_run=False,
    )

    assert strict_calls == [((repository,), False)]
    assert results[0]["action"] == "registered"
    assert skipped == []
    assert reload_required is True


def test_manual_repository_keeps_original_strict_onboarding():
    repository = "rhgo1749/ctrl-hangul"
    snapshot = {
        "schema_version": 2,
        "repositories": [
            {
                "repository": repository,
                "ready": True,
                "checkout_status": "verified",
                "checkout": "/ws/projects/ctrl-hangul",
                "default_branch": "main",
            }
        ],
    }
    fake = _fake_module(_event_scope(repository), snapshot)
    calls = []

    def strict(token, repositories, supplied_snapshot, *, dry_run):
        calls.append(tuple(repositories))
        return ([], [], False)

    fake._provision_scoped_checkouts = strict
    fake._intake_overlay_manual_repository = True
    mod._install_existing_ready_scope_overlay(fake)

    fake._provision_scoped_checkouts(
        "token",
        (repository,),
        {},
        dry_run=True,
    )

    assert calls == [(repository,)]


def _snapshot_fake_module(tmp_path: Path, *, remote_sha: str, github_sha: str):
    fake = ModuleType("fake_origin_snapshot")
    fake.IntakeError = mod._core.IntakeError
    fake.RepoSnapshot = mod._core.RepoSnapshot
    fake.ONBOARDING_CONTRACT_CANDIDATES = mod._core.ONBOARDING_CONTRACT_CANDIDATES
    fake._ONBOARDING_SHA = mod._core._ONBOARDING_SHA
    fake._path_has_symlink_component = lambda path: False
    fake._normalise_remote = mod._core._normalise_remote
    fake._github_token = lambda: "token"
    fake._onboarding_repository_metadata = lambda token, repository: _metadata(
        repository,
        github_sha,
    )
    fake._repository_onboarding_lock = lambda repository: nullcontext()

    checkout = tmp_path / "repo"
    checkout.mkdir()

    def run_git(path: str, *args: str):
        if args == ("rev-parse", "--show-toplevel"):
            return 0, str(checkout), ""
        if args == ("remote", "get-url", "origin"):
            return 0, "https://github.com/rhgo1749/ctrl-hangul.git", ""
        if args == ("rev-parse", "--verify", "origin/main^{commit}"):
            return 0, remote_sha, ""
        if args == ("cat-file", "-e", "origin/main:AGENTS.md"):
            return 0, "", ""
        raise AssertionError(args)

    fake._run_git = run_git
    fake._repo_snapshot = lambda config: (_ for _ in ()).throw(
        AssertionError("canonical strict snapshot must be replaced")
    )
    return fake, checkout


def test_origin_snapshot_accepts_shared_head_drift_when_origin_ref_is_fresh(tmp_path):
    sha = "c" * 40
    fake, checkout = _snapshot_fake_module(
        tmp_path,
        remote_sha=sha,
        github_sha=sha,
    )
    config = SimpleNamespace(
        name="rhgo1749/ctrl-hangul",
        checkout=str(checkout),
        default_branch="main",
        contract_paths=("AGENTS.md",),
    )

    mod._install_origin_snapshot_overlay(fake)
    snapshot = fake._repo_snapshot(config)

    assert snapshot.origin_sha == sha
    assert snapshot.remote == "https://github.com/rhgo1749/ctrl-hangul.git"
    assert snapshot.contract_paths == ("AGENTS.md",)


def test_origin_snapshot_rejects_stale_origin_ref(tmp_path):
    fake, checkout = _snapshot_fake_module(
        tmp_path,
        remote_sha="d" * 40,
        github_sha="e" * 40,
    )
    config = SimpleNamespace(
        name="rhgo1749/ctrl-hangul",
        checkout=str(checkout),
        default_branch="main",
        contract_paths=("AGENTS.md",),
    )

    mod._install_origin_snapshot_overlay(fake)

    with pytest.raises(fake.IntakeError, match="origin/main is stale"):
        fake._repo_snapshot(config)


def test_run_context_forwards_dry_run_and_manual_repository_flags():
    fake = ModuleType("fake_run_context")
    observed = []

    def run_once(args):
        observed.append(
            (
                fake._intake_overlay_dry_run,
                fake._intake_overlay_manual_repository,
            )
        )
        return 0

    fake._run_once = run_once
    mod._install_run_context_overlay(fake)

    assert fake._run_once(
        argparse.Namespace(dry_run=True, repository="rhgo1749/ctrl-hangul")
    ) == 0
    assert observed == [(True, True)]
    assert fake.__dict__.get("_intake_overlay_dry_run") is None
    assert fake.__dict__.get("_intake_overlay_manual_repository") is None
