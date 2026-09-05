#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "automation/hermes/scripts/github-agent-ready-kanban-intake.py"
spec = importlib.util.spec_from_file_location("intake_checkout_self_heal_test", SOURCE)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

EXPECTED_REMOTE = "https://github.com/acme/repo.git"


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def make_fixture(root: Path, *, shallow: bool = True, attributes: bool = False):
    bare = root / "remote.git"
    source = root / "source"
    git("init", "--bare", str(bare))
    git("init", "-b", "main", str(source))
    git("-C", str(source), "config", "user.email", "test@example.com")
    git("-C", str(source), "config", "user.name", "Test")
    (source / "AGENTS.md").write_text("contract\n", encoding="utf-8")
    (source / "value.txt").write_text("one\n", encoding="utf-8")
    if attributes:
        (source / ".gitattributes").write_text(
            "value.txt filter=repository-controlled\n",
            encoding="utf-8",
        )
    git("-C", str(source), "add", ".")
    git("-C", str(source), "commit", "-m", "one")
    git("-C", str(source), "remote", "add", "origin", str(bare))
    git("-C", str(source), "push", "origin", "main")
    first = git("-C", str(source), "rev-parse", "HEAD")

    checkout = root / "checkout"
    clone_args = ["clone"]
    if shallow:
        clone_args.extend(["--depth=1"])
    clone_args.extend(["--branch", "main", f"file://{bare}", str(checkout)])
    git(*clone_args)
    git("-C", str(checkout), "remote", "set-url", "origin", EXPECTED_REMOTE)

    (source / "value.txt").write_text("two\n", encoding="utf-8")
    git("-C", str(source), "add", ".")
    git("-C", str(source), "commit", "-m", "two")
    git("-C", str(source), "push", "origin", "main")
    target = git("-C", str(source), "rev-parse", "HEAD")
    metadata = mod.OnboardingRepository(
        "acme/repo",
        1,
        "main",
        target,
        ("AGENTS.md",),
    )
    return bare, checkout, first, target, metadata


def route_fetch(monkeypatch: pytest.MonkeyPatch, bare: Path):
    real_run = mod.subprocess.run

    def routed(command, **kwargs):
        args = list(command)
        if args and args[0] == "git" and "fetch" in args:
            args = [
                f"file://{bare}" if value in {"origin", EXPECTED_REMOTE} else value
                for value in args
            ]
        return real_run(args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", routed)


def test_shallow_clean_checkout_is_unshallowed_and_fast_forwarded(tmp_path, monkeypatch):
    bare, checkout, first, target, metadata = make_fixture(tmp_path)
    route_fetch(monkeypatch, bare)

    assert git("-C", str(checkout), "rev-parse", "--is-shallow-repository") == "true"
    assert mod._self_heal_stale_checkout("token", metadata, checkout) == "healed"
    assert git("-C", str(checkout), "rev-parse", "HEAD") == target
    assert git("-C", str(checkout), "rev-parse", "origin/main") == target
    assert git("-C", str(checkout), "rev-parse", "--is-shallow-repository") == "false"
    assert (checkout / "value.txt").read_text(encoding="utf-8") == "two\n"
    assert first != target


def test_non_shallow_clean_stale_checkout_is_fast_forwarded(tmp_path, monkeypatch):
    bare, checkout, _, target, metadata = make_fixture(tmp_path, shallow=False)
    route_fetch(monkeypatch, bare)

    assert mod._self_heal_stale_checkout("token", metadata, checkout) == "healed"
    assert git("-C", str(checkout), "rev-parse", "HEAD") == target
    assert (checkout / "value.txt").read_text(encoding="utf-8") == "two\n"


def test_exact_clean_sha_is_a_fetch_free_noop(tmp_path, monkeypatch):
    bare, checkout, _, target, metadata = make_fixture(tmp_path, shallow=False)
    git("-C", str(checkout), "remote", "set-url", "origin", f"file://{bare}")
    git("-C", str(checkout), "fetch", "origin", "main")
    git("-C", str(checkout), "merge", "--ff-only", "origin/main")
    git("-C", str(checkout), "remote", "set-url", "origin", EXPECTED_REMOTE)
    fetches: list[list[str]] = []
    real_run = mod.subprocess.run

    def record(command, **kwargs):
        args = list(command)
        if args and args[0] == "git" and "fetch" in args:
            fetches.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", record)
    assert mod._self_heal_stale_checkout("token", metadata, checkout) == "noop"
    assert fetches == []
    assert git("-C", str(checkout), "rev-parse", "HEAD") == target


def test_dirty_checkout_is_rejected_before_any_refresh(tmp_path, monkeypatch):
    bare, checkout, first, target, metadata = make_fixture(tmp_path)
    (checkout / "value.txt").write_text("dirty\n", encoding="utf-8")
    fetches: list[list[str]] = []
    real_run = mod.subprocess.run

    def record(command, **kwargs):
        args = list(command)
        if args and args[0] == "git" and "fetch" in args:
            fetches.append(args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", record)
    with pytest.raises(mod.IntakeError, match="^checkout_dirty$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert fetches == []
    assert git("-C", str(checkout), "rev-parse", "HEAD") == first
    assert (checkout / "value.txt").read_text(encoding="utf-8") == "dirty\n"


def test_diverged_checkout_keeps_canonical_refs_and_objects_unchanged(tmp_path, monkeypatch):
    bare, checkout, _, target, metadata = make_fixture(tmp_path, shallow=False)
    git("-C", str(checkout), "config", "user.email", "test@example.com")
    git("-C", str(checkout), "config", "user.name", "Test")
    (checkout / "value.txt").write_text("local\n", encoding="utf-8")
    git("-C", str(checkout), "add", ".")
    git("-C", str(checkout), "commit", "-m", "local")
    head_before = git("-C", str(checkout), "rev-parse", "HEAD")
    remote_before = git("-C", str(checkout), "rev-parse", "origin/main")
    objects = checkout / ".git" / "objects"

    def fingerprint() -> str:
        digest = hashlib.sha256()
        for path in sorted(objects.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(objects)).encode("utf-8"))
                digest.update(path.read_bytes())
        return digest.hexdigest()

    objects_before = fingerprint()
    route_fetch(monkeypatch, bare)
    with pytest.raises(mod.IntakeError, match="^checkout_diverged$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git("-C", str(checkout), "rev-parse", "HEAD") == head_before
    assert git("-C", str(checkout), "rev-parse", "origin/main") == remote_before
    assert fingerprint() == objects_before
    assert (checkout / "value.txt").read_text(encoding="utf-8") == "local\n"
    assert target != head_before


def test_merge_safety_boundary_does_not_run_post_merge_hook(tmp_path, monkeypatch):
    bare, checkout, _, target, metadata = make_fixture(tmp_path, shallow=False)
    sentinel = tmp_path / "hook-ran"
    hook = checkout / ".git" / "hooks" / "post-merge"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    hook.chmod(0o755)
    route_fetch(monkeypatch, bare)

    assert mod._self_heal_stale_checkout("token", metadata, checkout) == "healed"
    assert not sentinel.exists()
    assert git("-C", str(checkout), "rev-parse", "HEAD") == target


def test_attribute_driven_materialization_fails_closed(tmp_path, monkeypatch):
    bare, checkout, first, target, metadata = make_fixture(tmp_path, attributes=True)
    route_fetch(monkeypatch, bare)
    with pytest.raises(mod.IntakeError, match="^checkout_materialization_unsafe$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git("-C", str(checkout), "rev-parse", "HEAD") == first
    assert git("-C", str(checkout), "rev-parse", "origin/main") == first
    assert target != first


def test_wrong_origin_is_rejected_without_fetch(tmp_path, monkeypatch):
    bare, checkout, first, target, metadata = make_fixture(tmp_path)
    git("-C", str(checkout), "remote", "set-url", "origin", "https://github.com/other/repo.git")

    real_run = mod.subprocess.run

    def fail_fetch(command, **kwargs):
        if command and command[0] == "git" and "fetch" in command:
            raise AssertionError("wrong-origin checkout must not fetch")
        return real_run(command, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", fail_fetch)
    with pytest.raises(mod.IntakeError, match="^checkout_origin_mismatch$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git("-C", str(checkout), "rev-parse", "HEAD") == first
    assert target != first
