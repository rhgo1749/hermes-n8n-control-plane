from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import URLError

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
    archived: bool = False,
):
    def fake(
        token: str,
        path: str,
        params: dict | None = None,
        *,
        allow_not_found: bool = False,
    ):
        assert token == "unit-test-token"
        if path == API_ROOT:
            return {
                "id": 42,
                "full_name": REPOSITORY,
                "owner": {"login": "rhgo1749", "type": "User"},
                "archived": archived,
                "disabled": False,
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
        {"repository": "rhgo1749/blocked", "reason": "repository_not_opted_in"}
    ]
    assert reload_required is False
    assert calls == ["rhgo1749/blocked"]


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
