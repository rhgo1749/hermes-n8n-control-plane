from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "automation"
    / "hermes"
    / "scripts"
    / "github-agent-ready-kanban-intake.py"
)
spec = importlib.util.spec_from_file_location(
    "github_agent_ready_kanban_intake_onboarding",
    MODULE_PATH,
)
assert spec and spec.loader
intake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)


REPOSITORY = "rhgo1749/new-agent"
API_ROOT = "/repos/rhgo1749/new-agent"


def _run_git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _api_fake(
    sha: str,
    *,
    topics: tuple[str, ...] = ("hermes-agent",),
    contracts: tuple[str, ...] = ("AGENTS.md",),
    archived: object = False,
    disabled: object = False,
    repository_id: object = 42,
):
    def fake(
        token: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_not_found: bool = False,
    ):
        assert token == "unit-test-token"
        if path == API_ROOT:
            return {
                "id": repository_id,
                "full_name": REPOSITORY,
                "owner": {"login": "rhgo1749", "type": "User"},
                "archived": archived,
                "disabled": disabled,
                "default_branch": "main",
            }
        if path == f"{API_ROOT}/topics":
            return {"names": list(topics)}
        if path.startswith(f"{API_ROOT}/contents/"):
            candidate = path.removeprefix(f"{API_ROOT}/contents/")
            if candidate in contracts:
                assert params == {"ref": "main"}
                return {"type": "file", "name": candidate}
            if allow_not_found:
                return None
            raise AssertionError(path)
        if path == f"{API_ROOT}/git/ref/heads/main":
            return {"object": {"sha": sha}}
        raise AssertionError(path)

    return fake


def _metadata(sha: str) -> Any:
    return intake.OnboardingRepository(
        repository=REPOSITORY,
        repository_id=42,
        default_branch="main",
        default_branch_sha=sha,
        contract_paths=("AGENTS.md",),
    )


def test_onboarding_metadata_revalidates_opt_in_and_visible_contract(monkeypatch):
    monkeypatch.setenv("HERMES_GITHUB_OWNER", "rhgo1749")
    monkeypatch.setenv("HERMES_GITHUB_TOPIC", "hermes-agent")
    monkeypatch.setattr(
        intake,
        "_github_onboarding_json",
        _api_fake("a" * 40),
    )

    metadata = intake._onboarding_repository_metadata(
        "unit-test-token",
        REPOSITORY,
    )

    assert metadata.repository == REPOSITORY
    assert metadata.repository_id == 42
    assert metadata.default_branch == "main"
    assert metadata.default_branch_sha == "a" * 40
    assert metadata.contract_paths == ("AGENTS.md",)


def test_onboarding_github_get_retries_one_transport_failure(monkeypatch):
    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit=-1):
            return b'{"ok": true}'

    def fake_urlopen(request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise URLError("temporary")
        return Response()

    monkeypatch.setattr(intake, "urlopen", fake_urlopen)
    assert intake._github_onboarding_json("unit-test-token", "/repos/x/y") == {
        "ok": True
    }
    assert calls == 2


def test_onboarding_github_get_retries_one_server_failure(monkeypatch):
    calls = 0

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit=-1):
            return b'{"ok": true}'

    def fake_urlopen(request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HTTPError(
                request.full_url,
                503,
                "temporary",
                hdrs=cast(Any, None),
                fp=None,
            )
        return Response()

    monkeypatch.setattr(intake, "urlopen", fake_urlopen)
    assert intake._github_onboarding_json("unit-test-token", "/repos/x/y") == {
        "ok": True
    }
    assert calls == 2


def test_onboarding_github_get_exhaustion_is_bounded(monkeypatch):
    calls = 0

    def fake_urlopen(request, *, timeout):
        nonlocal calls
        calls += 1
        raise URLError("temporary")

    monkeypatch.setattr(intake, "urlopen", fake_urlopen)
    with pytest.raises(intake.IntakeError, match="repository_unavailable"):
        intake._github_onboarding_json("unit-test-token", "/repos/x/y")
    assert calls == 2


def test_onboarding_github_get_does_not_retry_client_error(monkeypatch):
    calls = 0

    def fake_urlopen(request, *, timeout):
        nonlocal calls
        calls += 1
        raise HTTPError(
            request.full_url,
            403,
            "forbidden",
            hdrs=cast(Any, None),
            fp=None,
        )

    monkeypatch.setattr(intake, "urlopen", fake_urlopen)
    with pytest.raises(intake.IntakeError, match="repository_unavailable"):
        intake._github_onboarding_json("unit-test-token", "/repos/x/y")
    assert calls == 1


def test_onboarding_metadata_rejects_non_boolean_status_and_boolean_id(monkeypatch):
    monkeypatch.setenv("HERMES_GITHUB_OWNER", "rhgo1749")
    monkeypatch.setenv("HERMES_GITHUB_TOPIC", "hermes-agent")

    fakes = (
        _api_fake("b" * 40, archived="false"),
        _api_fake("b" * 40, disabled=None),
        _api_fake("b" * 40, repository_id=True),
    )
    for fake in fakes:
        monkeypatch.setattr(intake, "_github_onboarding_json", fake)
        with pytest.raises(intake.IntakeError, match="repository_metadata_invalid"):
            intake._onboarding_repository_metadata("unit-test-token", REPOSITORY)


def test_onboarding_metadata_rejects_archived_or_unopted_repository(monkeypatch):
    monkeypatch.setenv("HERMES_GITHUB_OWNER", "rhgo1749")
    monkeypatch.setenv("HERMES_GITHUB_TOPIC", "hermes-agent")

    monkeypatch.setattr(
        intake,
        "_github_onboarding_json",
        _api_fake("b" * 40, archived=True),
    )
    with pytest.raises(intake.IntakeError, match="repository_archived"):
        intake._onboarding_repository_metadata("unit-test-token", REPOSITORY)

    monkeypatch.setattr(
        intake,
        "_github_onboarding_json",
        _api_fake("b" * 40, topics=("other-topic",)),
    )
    with pytest.raises(intake.IntakeError, match="repository_not_opted_in"):
        intake._onboarding_repository_metadata("unit-test-token", REPOSITORY)

    monkeypatch.setattr(
        intake,
        "_github_onboarding_json",
        _api_fake("b" * 40, contracts=()),
    )
    with pytest.raises(intake.IntakeError, match="contract_visibility_invalid"):
        intake._onboarding_repository_metadata("unit-test-token", REPOSITORY)


def test_onboarding_metadata_rejects_invalid_branch_and_never_echoes_token(
    monkeypatch,
):
    monkeypatch.setenv("HERMES_GITHUB_OWNER", "rhgo1749")
    monkeypatch.setenv("HERMES_GITHUB_TOPIC", "hermes-agent")
    real_github_json = intake._github_onboarding_json
    monkeypatch.setattr(
        intake,
        "_github_onboarding_json",
        lambda token, path, params=None, *, allow_not_found=False: {
            "id": 42,
            "full_name": REPOSITORY,
            "owner": {"login": "rhgo1749", "type": "User"},
            "archived": False,
            "disabled": False,
            "default_branch": "-unsafe",
        },
    )
    with pytest.raises(intake.IntakeError, match="default_branch_invalid") as error:
        intake._onboarding_repository_metadata("unit-test-token", REPOSITORY)
    assert "unit-test-token" not in str(error.value)

    monkeypatch.setattr(intake, "_github_onboarding_json", real_github_json)

    def unavailable(request, timeout):
        raise URLError("unit-test-token must not be copied")

    monkeypatch.setattr(intake, "urlopen", unavailable)
    with pytest.raises(intake.IntakeError, match="repository_unavailable") as error:
        intake._github_onboarding_json("unit-test-token", "/repos/x/y")
    assert "unit-test-token" not in str(error.value)


def test_ensure_checkout_reuses_clean_existing_checkout_and_rejects_dirty(
    monkeypatch,
    tmp_path: Path,
):
    checkout_root = tmp_path / "projects"
    checkout = checkout_root / "new-agent"
    checkout.mkdir(parents=True)
    _run_git("init", cwd=checkout)
    _run_git("config", "user.email", "unit@example.test", cwd=checkout)
    _run_git("config", "user.name", "Unit Test", cwd=checkout)
    (checkout / "AGENTS.md").write_text("# contract\n", encoding="utf-8")
    _run_git("add", "AGENTS.md", cwd=checkout)
    _run_git("commit", "-m", "contract", cwd=checkout)
    _run_git("branch", "-M", "main", cwd=checkout)
    _run_git(
        "remote",
        "add",
        "origin",
        "https://github.com/rhgo1749/new-agent.git",
        cwd=checkout,
    )
    _run_git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=checkout)
    sha = _run_git("rev-parse", "HEAD", cwd=checkout)

    monkeypatch.setenv("HERMES_REPOSITORY_CHECKOUT_ROOT", str(checkout_root))
    monkeypatch.setenv("HERMES_KANBAN_INTAKE_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(intake, "_onboarding_repository_metadata", lambda token, repository: _metadata(sha))

    outcome = intake._ensure_checkout("unit-test-token", REPOSITORY)
    assert outcome.action == "reused"
    assert outcome.checkout == str(checkout)

    (checkout / "untracked.txt").write_text("do not overwrite\n", encoding="utf-8")
    with pytest.raises(intake.IntakeError, match="checkout_dirty"):
        intake._ensure_checkout("unit-test-token", REPOSITORY)
    assert (checkout / "untracked.txt").read_text(encoding="utf-8") == "do not overwrite\n"


def test_onboarding_checkout_rejects_staged_index_only_change(tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _run_git("init", cwd=checkout)
    _run_git("config", "user.email", "unit@example.test", cwd=checkout)
    _run_git("config", "user.name", "Unit Test", cwd=checkout)
    contract = checkout / "AGENTS.md"
    contract.write_text("# original\n", encoding="utf-8")
    _run_git("add", "AGENTS.md", cwd=checkout)
    _run_git("commit", "-m", "contract", cwd=checkout)

    assert intake._onboarding_checkout_is_clean(checkout) is True

    contract.write_text("# staged only\n", encoding="utf-8")
    _run_git("add", "AGENTS.md", cwd=checkout)
    status = _run_git("status", "--porcelain", cwd=checkout)
    assert status == "M  AGENTS.md"

    assert intake._onboarding_checkout_is_clean(checkout) is False


def test_issue_intake_github_get_uses_shared_bounded_retry(monkeypatch):
    calls = 0

    class Response:
        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit=-1):
            return b'{"ok": true}'

    def fake_urlopen(request, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise URLError("temporary")
        return Response()

    monkeypatch.setattr(intake, "urlopen", fake_urlopen)
    payload, headers = intake._github_json(
        "unit-test-token",
        "/repos/rhgo1749/new-agent",
        {},
    )
    assert payload == {"ok": True}
    assert headers == {}
    assert calls == 2


def test_metadata_rejects_noncanonical_repository_identity_before_api(monkeypatch):
    called = False

    def forbidden(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid identity must fail before GitHub")

    monkeypatch.setattr(intake, "_github_onboarding_json", forbidden)
    with pytest.raises(intake.IntakeError, match="repository_identity_invalid"):
        intake._onboarding_repository_metadata("unit-test-token", " rhgo1749/new-agent")
    assert called is False


def test_onboarding_branch_component_rules_fail_closed():
    for branch in ("feature/.hidden", "feature/release.lock", "@"):
        assert intake._valid_onboarding_branch(branch) is False


def test_existing_checkout_validator_rejects_wrong_branch_stale_head_and_missing_contract(
    monkeypatch, tmp_path: Path
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    metadata = _metadata("a" * 40)

    def fake_git(path, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return 0, str(checkout), ""
        if args == ("remote", "get-url", "origin"):
            return 0, f"https://github.com/{REPOSITORY}.git", ""
        if args == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            return 0, "feature", ""
        raise AssertionError(args)

    monkeypatch.setattr(intake, "_git_onboarding", fake_git)
    with pytest.raises(intake.IntakeError, match="checkout_default_branch_invalid"):
        intake._validate_onboarding_checkout(metadata, checkout)

    def stale_git(path, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return 0, str(checkout), ""
        if args == ("remote", "get-url", "origin"):
            return 0, f"https://github.com/{REPOSITORY}.git", ""
        if args == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            return 0, "main", ""
        if args == ("rev-parse", "--verify", "refs/remotes/origin/main^{commit}"):
            return 0, "b" * 40, ""
        if args == ("rev-parse", "--verify", "HEAD^{commit}"):
            return 0, "c" * 40, ""
        raise AssertionError(args)

    monkeypatch.setattr(intake, "_git_onboarding", stale_git)
    with pytest.raises(intake.IntakeError, match="checkout_default_branch_mismatch"):
        intake._validate_onboarding_checkout(metadata, checkout)

    def missing_contract_git(path, *args):
        if args == ("rev-parse", "--show-toplevel"):
            return 0, str(checkout), ""
        if args == ("remote", "get-url", "origin"):
            return 0, f"https://github.com/{REPOSITORY}.git", ""
        if args == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            return 0, "main", ""
        if args == ("rev-parse", "--verify", "refs/remotes/origin/main^{commit}"):
            return 0, "a" * 40, ""
        if args == ("rev-parse", "--verify", "HEAD^{commit}"):
            return 0, "a" * 40, ""
        if args == ("status", "--porcelain", "--untracked-files=all", "--ignored=matching"):
            return 0, "", ""
        if args == ("cat-file", "-e", "refs/remotes/origin/main:AGENTS.md"):
            return 1, "", "missing"
        raise AssertionError(args)

    monkeypatch.setattr(intake, "_git_onboarding", missing_contract_git)
    monkeypatch.setattr(intake, "_onboarding_checkout_is_clean", lambda checkout: True)
    with pytest.raises(intake.IntakeError, match="contract_visibility_invalid"):
        intake._validate_onboarding_checkout(metadata, checkout)


def test_mutating_bootstrap_provision_requires_strict_validation_token():
    entry = {
        "repository": REPOSITORY,
        "bootstrap": {
            "board": "new-agent",
            "checkout": "/ws/projects/new-agent",
        },
    }
    with pytest.raises(intake.IntakeError, match="checkout_validation_unavailable"):
        intake._provision_bootstrap_boards(
            {"repositories": [entry]},
            dry_run=False,
        )


def test_strict_bootstrap_validation_rechecks_metadata_and_checkout_under_lock(
    monkeypatch, tmp_path: Path
):
    root = tmp_path / "projects"
    checkout = root / "new-agent"
    monkeypatch.setenv("HERMES_REPOSITORY_CHECKOUT_ROOT", str(root))
    monkeypatch.setenv("HERMES_KANBAN_INTAKE_HOME", str(tmp_path / "hermes"))
    metadata_calls = []
    checkout_calls = []

    def metadata(token, repository):
        metadata_calls.append((token, repository))
        return _metadata("a" * 40)

    def validate(value, path):
        checkout_calls.append((value, path))

    monkeypatch.setattr(intake, "_onboarding_repository_metadata", metadata)
    monkeypatch.setattr(intake, "_validate_onboarding_checkout", validate)

    intake._strict_validate_bootstrap_checkout("unit-test-token", REPOSITORY, checkout)

    assert metadata_calls == [("unit-test-token", REPOSITORY)]
    assert checkout_calls == [(_metadata("a" * 40), checkout)]


def test_repository_lock_serializes_same_checkout_registration(monkeypatch, tmp_path: Path):
    root = tmp_path / "projects"
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_REPOSITORY_CHECKOUT_ROOT", str(root))
    monkeypatch.setenv("HERMES_KANBAN_INTAKE_HOME", str(home))
    sha = "d" * 40
    monkeypatch.setattr(
        intake,
        "_onboarding_repository_metadata",
        lambda token, repository: _metadata(sha),
    )
    monkeypatch.setattr(intake, "_validate_onboarding_checkout", lambda metadata, checkout: None)
    clone_calls = 0
    clone_lock = threading.Lock()

    def fake_clone(token, metadata, checkout_root, destination):
        nonlocal clone_calls
        with clone_lock:
            clone_calls += 1
        destination.mkdir(parents=True)

    monkeypatch.setattr(intake, "_clone_onboarding_checkout", fake_clone)
    barrier = threading.Barrier(2)
    outcomes = []
    errors = []

    def worker():
        try:
            barrier.wait(timeout=5)
            outcomes.append(intake._ensure_checkout("unit-test-token", REPOSITORY))
        except (intake.IntakeError, threading.BrokenBarrierError, TimeoutError) as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(outcomes) == 2
    assert sorted(outcome.action for outcome in outcomes) == ["registered", "reused"]
    assert clone_calls == 1


def test_archive_materialization_rejects_noncanonical_member_paths(tmp_path: Path):
    for member_name in ("../escape", "dir//file", "dir/./file", "dir\\file"):
        with pytest.raises(intake.IntakeError, match="clone_failed"):
            intake._archive_member_path(tmp_path, member_name)


def test_dry_run_does_not_create_checkout(monkeypatch, tmp_path: Path):
    checkout_root = tmp_path / "projects"
    monkeypatch.setenv("HERMES_REPOSITORY_CHECKOUT_ROOT", str(checkout_root))
    monkeypatch.setenv("HERMES_KANBAN_INTAKE_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        intake,
        "_onboarding_repository_metadata",
        lambda token, repository: _metadata("c" * 40),
    )

    outcome = intake._ensure_checkout(
        "unit-test-token",
        REPOSITORY,
        dry_run=True,
    )

    assert outcome.action == "would_register"
    assert outcome.checkout == str(checkout_root / "new-agent")
    assert not (checkout_root / "new-agent").exists()


def test_checkout_root_symlink_is_rejected(monkeypatch, tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "projects"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("HERMES_REPOSITORY_CHECKOUT_ROOT", str(link))

    with pytest.raises(intake.IntakeError, match="checkout_path_conflict"):
        intake._checkout_path_for_onboarding(REPOSITORY, link)


def test_scoped_provisioning_skips_ready_entries_and_reports_safe_failures(
    monkeypatch,
):
    snapshot = {
        "repositories": [
            {"repository": REPOSITORY, "ready": True},
            {"repository": "rhgo1749/blocked", "ready": False},
        ]
    }
    calls = []

    def fake_ensure(token, repository, *, dry_run=False):
        calls.append(repository)
        raise intake.IntakeError("repository_not_opted_in")

    monkeypatch.setattr(intake, "_ensure_checkout", fake_ensure)
    results, skipped, reload_required = intake._provision_scoped_checkouts(
        "unit-test-token",
        (REPOSITORY, "rhgo1749/blocked"),
        snapshot,
        dry_run=False,
    )

    assert results == []
    assert skipped == [
        {"repository": REPOSITORY, "reason": "repository_not_opted_in"},
        {"repository": "rhgo1749/blocked", "reason": "repository_not_opted_in"},
    ]
    assert reload_required is False
    assert calls == [REPOSITORY, "rhgo1749/blocked"]


def test_scoped_provisioning_retains_partial_progress_on_unexpected_failure(monkeypatch):
    results = iter(
        [
            intake.CheckoutProvisioning(REPOSITORY, "/ws/projects/new-agent", "registered"),
        ]
    )

    def fake_ensure(token, repository, *, dry_run=False):
        del token, dry_run
        if repository == REPOSITORY:
            return next(results)
        raise RuntimeError("registry read failed")

    monkeypatch.setattr(intake, "_active_scope_progress", {})
    monkeypatch.setattr(intake, "_ensure_checkout", fake_ensure)

    with pytest.raises(RuntimeError, match="registry read failed"):
        intake._provision_scoped_checkouts(
            "unit-test-token",
            (REPOSITORY, "rhgo1749/other"),
            {"repositories": []},
            dry_run=False,
        )

    assert intake._onboarding_progress_is_partial() is True
    assert intake._active_scope_progress["checkout_provisioning"] == [
        {
            "repository": REPOSITORY,
            "checkout": "/ws/projects/new-agent",
            "action": "registered",
        }
    ]


def test_casefold_checkout_collision_is_rejected_without_mutation(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    foreign = root / "New-Agent"
    foreign.mkdir()
    marker = foreign / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    checkout = intake._checkout_path_for_onboarding(REPOSITORY, root)

    with pytest.raises(intake.IntakeError, match="checkout_path_conflict"):
        intake._reject_casefold_checkout_collision(root, checkout)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_missing_checkout_clones_agent_owned_temp_and_registers_atomically(
    monkeypatch,
    tmp_path: Path,
):
    source = tmp_path / "source"
    source.mkdir()
    _run_git("init", cwd=source)
    _run_git("config", "user.email", "unit@example.test", cwd=source)
    _run_git("config", "user.name", "Unit Test", cwd=source)
    (source / "AGENTS.md").write_text("# contract\n", encoding="utf-8")
    _run_git("add", "AGENTS.md", cwd=source)
    _run_git("commit", "-m", "contract", cwd=source)
    _run_git("branch", "-M", "main", cwd=source)
    sha = _run_git("rev-parse", "HEAD", cwd=source)

    checkout_root = tmp_path / "projects"
    monkeypatch.setenv("HERMES_REPOSITORY_CHECKOUT_ROOT", str(checkout_root))
    monkeypatch.setenv("HERMES_KANBAN_INTAKE_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        intake,
        "_onboarding_repository_metadata",
        lambda token, repository: _metadata(sha),
    )

    original_run = intake.subprocess.run

    def run(command, *args, **kwargs):
        command = list(command)
        is_clone = len(command) >= 2 and command[:2] == ["git", "clone"]
        if is_clone:
            command[5] = str(source)
        completed = original_run(command, *args, **kwargs)
        if is_clone and completed.returncode == 0:
            original_run(
                [
                    "git",
                    "-C",
                    command[-1],
                    "remote",
                    "set-url",
                    "origin",
                    f"https://github.com/{REPOSITORY}.git",
                ],
                check=True,
            )
        return completed

    monkeypatch.setattr(intake.subprocess, "run", run)
    outcome = intake._ensure_checkout("unit-test-token", REPOSITORY)

    checkout = checkout_root / "new-agent"
    assert outcome.action == "registered"
    assert (checkout / "AGENTS.md").is_file()
    assert not [path for path in checkout_root.iterdir() if path.name.startswith(".repository-onboarding-")]


def test_clone_materialization_does_not_execute_repository_filter(
    monkeypatch,
    tmp_path: Path,
):
    source = tmp_path / "source"
    source.mkdir()
    _run_git("init", cwd=source)
    _run_git("config", "user.email", "unit@example.test", cwd=source)
    _run_git("config", "user.name", "Unit Test", cwd=source)
    (source / "AGENTS.md").write_text("# contract\n", encoding="utf-8")
    (source / ".gitattributes").write_text("payload.txt filter=evil\n", encoding="utf-8")
    (source / "payload.txt").write_text("safe content\n", encoding="utf-8")
    (source / "link").symlink_to("payload.txt")
    _run_git("add", "AGENTS.md", ".gitattributes", "payload.txt", "link", cwd=source)
    _run_git("commit", "-m", "contract", cwd=source)
    _run_git("branch", "-M", "main", cwd=source)
    sha = _run_git("rev-parse", "HEAD", cwd=source)

    sentinel = tmp_path / "smudge-ran"
    hook_sentinel = tmp_path / "hook-ran"
    filter_script = tmp_path / "smudge.sh"
    filter_script.write_text(
        f"#!/bin/sh\nprintf ran > {str(sentinel)!r}\ncat\n",
        encoding="utf-8",
    )
    filter_script.chmod(0o700)
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    post_checkout = hooks_dir / "post-checkout"
    post_checkout.write_text(
        f"#!/bin/sh\nprintf ran > {str(hook_sentinel)!r}\n",
        encoding="utf-8",
    )
    post_checkout.chmod(0o700)

    checkout_root = tmp_path / "projects"
    monkeypatch.setenv("HERMES_REPOSITORY_CHECKOUT_ROOT", str(checkout_root))
    monkeypatch.setenv("HERMES_KANBAN_INTAKE_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(
        intake,
        "_onboarding_repository_metadata",
        lambda token, repository: _metadata(sha),
    )

    original_run = intake.subprocess.run
    clone_env: dict[str, str] = {}

    def run(command, *args, **kwargs):
        command = list(command)
        is_clone = len(command) >= 2 and command[:2] == ["git", "clone"]
        if is_clone:
            command[5] = str(source)
            clone_env.update(kwargs["env"])
        completed = original_run(command, *args, **kwargs)
        if is_clone and completed.returncode == 0:
            original_run(
                [
                    "git",
                    "-C",
                    command[-1],
                    "config",
                    "filter.evil.smudge",
                    str(filter_script),
                ],
                check=True,
            )
            original_run(
                [
                    "git",
                    "-C",
                    command[-1],
                    "config",
                    "core.hooksPath",
                    str(hooks_dir),
                ],
                check=True,
            )
            original_run(
                [
                    "git",
                    "-C",
                    command[-1],
                    "remote",
                    "set-url",
                    "origin",
                    f"https://github.com/{REPOSITORY}.git",
                ],
                check=True,
            )
        return completed

    monkeypatch.setattr(intake.subprocess, "run", run)
    outcome = intake._ensure_checkout("unit-test-token", REPOSITORY)

    assert outcome.action == "registered"
    assert not sentinel.exists()
    assert not hook_sentinel.exists()
    assert clone_env["GIT_CONFIG_COUNT"] == "2"
    assert clone_env["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert clone_env["GIT_CONFIG_KEY_1"] == "core.fsmonitor"
    assert clone_env["GIT_CONFIG_VALUE_1"] == "false"
    assert "GIT_TEMPLATE_DIR" not in clone_env
    assert "GIT_SSL_NO_VERIFY" not in clone_env
    assert (checkout_root / "new-agent" / "link").is_symlink()



def test_onboarding_checkout_allows_ignored_artifacts_but_rejects_ordinary_untracked(
    tmp_path: Path,
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    _run_git("init", cwd=checkout)
    _run_git("config", "user.email", "unit@example.test", cwd=checkout)
    _run_git("config", "user.name", "Unit Test", cwd=checkout)

    (checkout / "AGENTS.md").write_text(
        "# contract\n",
        encoding="utf-8",
    )
    (checkout / ".gitignore").write_text(
        "build/\n.gradle/\n",
        encoding="utf-8",
    )

    _run_git("add", "AGENTS.md", ".gitignore", cwd=checkout)
    _run_git("commit", "-m", "initial", cwd=checkout)

    info_exclude = checkout / ".git" / "info" / "exclude"
    with info_exclude.open("a", encoding="utf-8") as handle:
        handle.write("\n/.worktrees/\n")

    worktree_artifact = checkout / ".worktrees" / "task-1"
    worktree_artifact.mkdir(parents=True)
    (worktree_artifact / "marker").write_text(
        "managed artifact\n",
        encoding="utf-8",
    )

    (checkout / "build").mkdir()
    (checkout / "build" / "generated.bin").write_bytes(b"generated")

    (checkout / ".gradle").mkdir()
    (checkout / ".gradle" / "cache.bin").write_bytes(b"cache")

    # Ignored build/runtime/Hermes artifacts are compatible with a clean
    # canonical Git checkout.
    assert intake._onboarding_checkout_is_clean(checkout) is True

    # A genuinely ordinary untracked file remains fail-closed.
    (checkout / "scratch.txt").write_text(
        "unexpected\n",
        encoding="utf-8",
    )
    assert intake._onboarding_checkout_is_clean(checkout) is False
