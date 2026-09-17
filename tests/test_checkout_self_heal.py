#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
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


def make_fixture(
    root: Path, *, shallow: bool = True, attributes: bool = False, partial: bool = False,
):
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
    if partial:
        git("-C", str(bare), "config", "uploadpack.allowFilter", "true")
        clone_args.append("--filter=blob:none")
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


def make_detached_ancestor_fixture(root: Path):
    bare, checkout, first, second, _ = make_fixture(root, shallow=False)
    source = root / "source"

    # Advance the existing local and remote-tracking main refs to B.  The
    # later source commit C is the GitHub target and must not be present yet.
    git("-C", str(checkout), "remote", "set-url", "origin", f"file://{bare}")
    git("-C", str(checkout), "fetch", "origin", "main")
    git("-C", str(checkout), "merge", "--ff-only", "origin/main")
    git("-C", str(checkout), "remote", "set-url", "origin", EXPECTED_REMOTE)
    assert git("-C", str(checkout), "rev-parse", "refs/heads/main^{commit}") == second
    assert git("-C", str(checkout), "rev-parse", "origin/main") == second
    git("-C", str(checkout), "switch", "--detach", first)

    (source / "value.txt").write_text("three\n", encoding="utf-8")
    git("-C", str(source), "add", ".")
    git("-C", str(source), "commit", "-m", "three")
    git("-C", str(source), "push", "origin", "main")
    target = git("-C", str(source), "rev-parse", "HEAD")
    metadata = mod.OnboardingRepository(
        "acme/repo",
        1,
        "main",
        target,
        ("AGENTS.md",),
    )
    return bare, checkout, first, second, target, metadata


def object_fingerprint(checkout: Path) -> str:
    objects = checkout / ".git" / "objects"
    digest = hashlib.sha256()
    for path in sorted(objects.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(objects)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def git_state(checkout: Path) -> tuple[object, ...]:
    symbolic = subprocess.run(
        ["git", "-C", str(checkout), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        symbolic.returncode,
        symbolic.stdout.strip(),
        git("-C", str(checkout), "rev-parse", "HEAD^{commit}"),
        git("-C", str(checkout), "rev-parse", "refs/heads/main^{commit}"),
        git("-C", str(checkout), "rev-parse", "origin/main^{commit}"),
        git("-C", str(checkout), "status", "--porcelain=v1", "--untracked-files=all"),
        (checkout / "value.txt").read_bytes(),
        object_fingerprint(checkout),
    )


@pytest.mark.parametrize("promisor", ("true", "TRUE"))
def test_partial_clone_hydrates_only_at_authenticated_merge(tmp_path, monkeypatch, promisor):
    bare, checkout, first, target, metadata = make_fixture(
        tmp_path, shallow=False, partial=True,
    )
    git("-C", str(checkout), "config", "remote.origin.promisor", promisor)
    config_before = (checkout / ".git/config").read_bytes()
    sentinel = tmp_path / "hook-ran"
    hook = checkout / ".git/hooks/post-merge"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    hook.chmod(0o700)
    blob = git("-C", str(tmp_path / "source"), "rev-parse", f"{target}:value.txt")
    real_run = mod.subprocess.run
    events = []

    def routed(command, **kwargs):
        args = list(command)
        if args and args[0] == "git" and "env" in kwargs:
            env = kwargs["env"]
            if "merge" in args:
                events.append("merge")
                assert env.get("GIT_ONBOARDING_TOKEN") == "token"
                assert Path(env["GIT_ASKPASS"]).is_file()
                assert env.get("GIT_NO_LAZY_FETCH") == "0"
                assert "--ff-only" in args and "--no-verify" in args
            elif "fetch" in args or "merge-base" in args:
                assert env.get("GIT_NO_LAZY_FETCH") == "1"
            # Test-only transport routing, including Git's child lazy fetch.
            # Keep origin and partial-clone config on disk byte-identical.
            if "fetch" in args or "merge" in args:
                # Route the canonical HTTPS remote to the local fixture without
                # changing the checkout's origin or partial-clone config.
                args[1:1] = [
                    "-c",
                    f"url.file://{bare}.insteadOf={EXPECTED_REMOTE}",
                ]
            result = real_run(args, **kwargs)
            if "fetch" in args and str(checkout) in args:
                events.append("fetch")
                probe = real_run(
                    ["git", "-C", str(checkout), "cat-file", "-e", blob],
                    env={**env, "GIT_NO_LAZY_FETCH": "1"}, capture_output=True,
                )
                assert probe.returncode != 0, "exact fetch must leave target blob absent"
            return result
        return real_run(args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", routed)
    assert mod._self_heal_stale_checkout("token", metadata, checkout) == "healed"
    assert events == ["fetch", "merge"]
    assert first != target
    assert git_state(checkout)[2:7] == (target, target, target, "", b"two\n")
    assert (checkout / ".git/config").read_bytes() == config_before
    assert not sentinel.exists()
    events.clear()
    assert mod._self_heal_stale_checkout("token", metadata, checkout) == "noop"
    assert events == []


def test_full_and_partial_clone_stale_checkouts_converge(tmp_path, monkeypatch):
    full_root = tmp_path / "full"
    partial_root = tmp_path / "partial"
    full_root.mkdir()
    partial_root.mkdir()
    full_bare, full_checkout, _, full_target, full_metadata = make_fixture(
        full_root, shallow=False,
    )
    partial_bare, partial_checkout, _, partial_target, partial_metadata = make_fixture(
        partial_root, shallow=False, partial=True,
    )
    real_run = mod.subprocess.run

    def heal(bare, checkout, metadata):
        def routed(command, **kwargs):
            args = list(command)
            if args and args[0] == "git" and ("fetch" in args or "merge" in args):
                args[1:1] = [
                    "-c",
                    f"url.file://{bare}.insteadOf={EXPECTED_REMOTE}",
                ]
            return real_run(args, **kwargs)

        monkeypatch.setattr(mod.subprocess, "run", routed)
        assert mod._self_heal_stale_checkout("token", metadata, checkout) == "healed"

    heal(full_bare, full_checkout, full_metadata)
    heal(partial_bare, partial_checkout, partial_metadata)

    full_state = git_state(full_checkout)
    partial_state = git_state(partial_checkout)
    assert full_state[:2] == partial_state[:2] == (0, "main")
    assert full_state[5:7] == partial_state[5:7] == ("", b"two\n")
    assert full_state[2:5] == (full_target, full_target, full_target)
    assert partial_state[2:5] == (partial_target, partial_target, partial_target)


@pytest.mark.parametrize("entries", [
    [("promisor", "true")], [("partialclonefilter", "blob:none")],
    *[[("promisor", value), ("partialclonefilter", "blob:none")]
      for value in ("", "false", "yes", "on", "1")],
    *[[("promisor", "true"), ("partialclonefilter", value)]
      for value in ("", "blob:limit=10", "tree:0", "sparse:oid=HEAD", "combine:blob:none+tree:0")],
    [("promisor", "true"), ("promisor", "true"), ("partialclonefilter", "blob:none")],
    [("promisor", "true"), ("partialclonefilter", "blob:none"), ("partialclonefilter", "blob:none")],
])
def test_partial_clone_invalid_metadata_is_mutation_free(tmp_path, monkeypatch, entries):
    _, checkout, _, _, metadata = make_fixture(tmp_path, shallow=False)
    for key, value in entries:
        git("-C", str(checkout), "config", "--add", f"remote.origin.{key}", value)
    before = git_state(checkout)
    config_before = (checkout / ".git/config").read_bytes()
    real_run = mod.subprocess.run

    def reject_fetch(command, **kwargs):
        assert "fetch" not in command
        return real_run(command, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", reject_fetch)
    with pytest.raises(mod.IntakeError, match="^checkout_materialization_unsafe$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git_state(checkout) == before
    assert (checkout / ".git/config").read_bytes() == config_before


@pytest.mark.parametrize("key", ("remote.origin.uploadpack", "remote.foreign.url"))
def test_partial_clone_retains_remote_execution_rejection(tmp_path, monkeypatch, key):
    _, checkout, _, _, metadata = make_fixture(tmp_path, shallow=False, partial=True)
    sentinel = tmp_path / "helper-ran"
    helper = tmp_path / "helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch {sentinel}\nexit 1\n", encoding="utf-8")
    helper.chmod(0o700)
    git("-C", str(checkout), "config", key, str(helper))
    before = git_state(checkout)
    real_run = mod.subprocess.run

    def reject_fetch(command, **kwargs):
        assert "fetch" not in command
        return real_run(command, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", reject_fetch)
    with pytest.raises(mod.IntakeError, match="^checkout_materialization_unsafe$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git_state(checkout) == before
    assert not sentinel.exists()


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
    _bare, checkout, first, _target, metadata = make_fixture(tmp_path)
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
    _bare, checkout, first, _target, metadata = make_fixture(tmp_path)
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
    assert _target != first


def test_detached_ancestor_reattaches_and_fast_forwards(tmp_path, monkeypatch):
    bare, checkout, first, second, target, metadata = make_detached_ancestor_fixture(tmp_path)
    route_fetch(monkeypatch, bare)
    sentinel = tmp_path / "post-checkout-ran"
    hook = checkout / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\nprintf ran > {str(sentinel)!r}\n", encoding="utf-8")
    hook.chmod(0o700)


    assert git("-C", str(checkout), "rev-parse", "HEAD") == first
    assert mod._self_heal_stale_checkout("token", metadata, checkout) == "healed"
    assert not sentinel.exists()
    assert git("-C", str(checkout), "symbolic-ref", "--short", "HEAD") == "main"
    assert git("-C", str(checkout), "rev-parse", "HEAD") == target
    assert git("-C", str(checkout), "rev-parse", "refs/heads/main^{commit}") == target
    assert git("-C", str(checkout), "rev-parse", "origin/main^{commit}") == target
    assert (checkout / "value.txt").read_text(encoding="utf-8") == "three\n"
    assert second != target


def test_detached_unique_commit_is_rejected_without_mutation(tmp_path, monkeypatch):
    bare, checkout, first, second, _, metadata = make_detached_ancestor_fixture(tmp_path)
    git("-C", str(checkout), "config", "user.email", "test@example.com")
    git("-C", str(checkout), "config", "user.name", "Test")
    git("-C", str(checkout), "switch", "--detach", first)
    (checkout / "value.txt").write_text("detached-only\n", encoding="utf-8")
    git("-C", str(checkout), "add", ".")
    git("-C", str(checkout), "commit", "-m", "detached-only")
    before = git_state(checkout)
    route_fetch(monkeypatch, bare)

    with pytest.raises(mod.IntakeError, match="^checkout_default_branch_invalid$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git_state(checkout) == before
    assert git("-C", str(checkout), "rev-parse", "refs/heads/main^{commit}") == second


def test_detached_missing_local_default_is_rejected_without_mutation(tmp_path, monkeypatch):
    bare, checkout, _, _, _, metadata = make_detached_ancestor_fixture(tmp_path)
    git("-C", str(checkout), "branch", "-D", "main")
    before = git_state_without_local_default(checkout)
    route_fetch(monkeypatch, bare)

    with pytest.raises(mod.IntakeError, match="^checkout_default_branch_invalid$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git_state_without_local_default(checkout) == before


def test_detached_default_owned_by_other_worktree_is_rejected_without_mutation(
    tmp_path,
    monkeypatch,
):
    bare, checkout, _, _, _, metadata = make_detached_ancestor_fixture(tmp_path)
    other = tmp_path / "other-worktree"
    git("-C", str(checkout), "worktree", "add", "--quiet", str(other), "main")
    other_before = (
        git("-C", str(other), "rev-parse", "HEAD"),
        (other / "value.txt").read_bytes(),
    )
    before = git_state(checkout)
    route_fetch(monkeypatch, bare)

    with pytest.raises(mod.IntakeError, match="^checkout_default_branch_invalid$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git_state(checkout) == before
    assert (
        git("-C", str(other), "rev-parse", "HEAD"),
        (other / "value.txt").read_bytes(),
    ) == other_before


def test_detached_local_default_divergence_is_rejected_before_switch(tmp_path, monkeypatch):
    bare, checkout, first, _, _, metadata = make_detached_ancestor_fixture(tmp_path)
    git("-C", str(checkout), "switch", "main")
    git("-C", str(checkout), "config", "user.email", "test@example.com")
    git("-C", str(checkout), "config", "user.name", "Test")
    (checkout / "value.txt").write_text("local-divergence\n", encoding="utf-8")
    git("-C", str(checkout), "add", ".")
    git("-C", str(checkout), "commit", "-m", "local-divergence")
    git("-C", str(checkout), "switch", "--detach", first)
    before = git_state(checkout)
    route_fetch(monkeypatch, bare)

    with pytest.raises(mod.IntakeError, match="^checkout_diverged$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git_state(checkout) == before


@pytest.mark.parametrize("dirty_kind", ("staged", "untracked"))
def test_detached_dirty_checkout_is_rejected_without_mutation(
    tmp_path,
    monkeypatch,
    dirty_kind,
):
    bare, checkout, _, _, _, metadata = make_detached_ancestor_fixture(tmp_path)
    if dirty_kind == "staged":
        (checkout / "value.txt").write_text("staged\n", encoding="utf-8")
        git("-C", str(checkout), "add", "value.txt")
    else:
        (checkout / "ordinary-untracked.txt").write_text("untracked\n", encoding="utf-8")
    before = git_state(checkout)
    route_fetch(monkeypatch, bare)

    with pytest.raises(mod.IntakeError, match="^checkout_dirty$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert git_state(checkout) == before


def git_state_without_local_default(checkout: Path) -> tuple[object, ...]:
    symbolic = subprocess.run(
        ["git", "-C", str(checkout), "symbolic-ref", "--quiet", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    remote = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--verify", "origin/main^{commit}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (
        symbolic.returncode,
        symbolic.stdout.strip(),
        git("-C", str(checkout), "rev-parse", "HEAD^{commit}"),
        remote.returncode,
        remote.stdout.strip(),
        git("-C", str(checkout), "status", "--porcelain=v1", "--untracked-files=all"),
        (checkout / "value.txt").read_bytes(),
        object_fingerprint(checkout),
    )


@pytest.mark.parametrize("attribute_source", ("info", "configured"))
def test_local_attribute_sources_fail_closed_before_switch(
    tmp_path,
    monkeypatch,
    attribute_source,
):
    bare, checkout, _, _, _, metadata = make_detached_ancestor_fixture(tmp_path)
    sentinel = tmp_path / "smudge-ran"
    filter_script = tmp_path / "smudge.sh"
    filter_script.write_text(
        f"#!/bin/sh\nprintf ran > {str(sentinel)!r}\ncat\n",
        encoding="utf-8",
    )
    filter_script.chmod(0o700)
    if attribute_source == "info":
        (checkout / ".git" / "info" / "attributes").write_text(
            "value.txt filter=probe\n",
            encoding="utf-8",
        )
    else:
        attributes_file = tmp_path / "attributes"
        attributes_file.write_text("value.txt filter=probe\n", encoding="utf-8")
        git("-C", str(checkout), "config", "core.attributesFile", str(attributes_file))
        git("-C", str(checkout), "config", "filter.probe.smudge", str(filter_script))
    before = git_state(checkout)
    route_fetch(monkeypatch, bare)

    with pytest.raises(mod.IntakeError, match="^checkout_materialization_unsafe$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert not sentinel.exists()
    assert git_state(checkout) == before


@pytest.mark.parametrize("unsafe_key", ("url", "remote-helper"))
def test_local_transport_configuration_fails_closed_before_fetch(
    tmp_path,
    monkeypatch,
    unsafe_key,
):
    bare, checkout, _, _, _, metadata = make_detached_ancestor_fixture(tmp_path)
    sentinel = tmp_path / "remote-helper-ran"
    helper = tmp_path / "remote-helper.sh"
    helper.write_text(
        f"#!/bin/sh\nprintf ran > {str(sentinel)!r}\nexit 1\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    if unsafe_key == "url":
        git(
            "-C",
            str(checkout),
            "config",
            f"url.file://{bare}/.insteadOf",
            "https://github.com/",
        )
    else:
        git("-C", str(checkout), "config", "remote.origin.uploadpack", str(helper))
    before = git_state(checkout)
    route_fetch(monkeypatch, bare)

    with pytest.raises(mod.IntakeError, match="^checkout_materialization_unsafe$"):
        mod._self_heal_stale_checkout("token", metadata, checkout)
    assert not sentinel.exists()
    assert git_state(checkout) == before
