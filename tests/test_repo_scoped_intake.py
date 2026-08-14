#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "automation"
    / "hermes"
    / "scripts"
    / "github-agent-ready-kanban-intake.py"
)

spec = importlib.util.spec_from_file_location(
    "github_agent_ready_kanban_intake",
    MODULE_PATH,
)
assert spec and spec.loader
intake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)


def _config(
    repository: str,
    *,
    board: str | None = None,
    checkout: str = "/tmp/repo",
    default_branch: str = "main",
    contract_paths: tuple[str, ...] = (),
):
    return intake.RepositoryConfig(
        name=repository,
        board=board or repository.split("/", 1)[1].casefold(),
        checkout=checkout,
        default_branch=default_branch,
        contract_paths=contract_paths,
    )


def _entry(
    repository: str,
    *,
    ready: bool = True,
    board: str | None = None,
    checkout: str | None = None,
    default_branch: str = "main",
    contract_paths: list[str] | None = None,
    reason: str | None = None,
):
    slug = repository.split("/", 1)[1].casefold()
    return {
        "repository": repository,
        "repository_id": 1,
        "default_branch": default_branch,
        "canonical_slug": slug,
        "board": board or slug,
        "board_status": (
            "resolved_task_provenance"
            if ready
            else "not_found_task_provenance"
        ),
        "checkout": checkout or f"/ws/projects/{slug}",
        "checkout_status": "verified" if ready else "missing",
        "checkout_remote": f"https://github.com/{repository}.git",
        "contract_paths": contract_paths or [],
        "ready": ready,
        "reason": reason if not ready else None,
    }


def _snapshot(entries: list[dict]) -> dict:
    return {
        "schema_version": 2,
        "mode": "shadow",
        "board_authority": "tasks.idempotency_key",
        "repositories": entries,
    }


def test_default_scope_keeps_all_ready_registry_configs() -> None:
    configs = (
        _config("rhgo1749/ctrl-hangul", board="ctrlhangul"),
        _config("rhgo1749/re-bound"),
    )
    assert intake._select_repositories(configs, None) == configs


def test_repository_scope_is_case_insensitive() -> None:
    configs = (
        _config("rhgo1749/ctrl-hangul", board="ctrlhangul"),
        _config("rhgo1749/re-bound"),
    )
    selected = intake._select_repositories(
        configs,
        "RHGO1749/CTRL-HANGUL",
    )
    assert len(selected) == 1
    assert selected[0].name == "rhgo1749/ctrl-hangul"
    assert selected[0].board == "ctrlhangul"


def test_unknown_repository_fails_closed() -> None:
    configs = (_config("rhgo1749/ctrl-hangul"),)
    try:
        intake._select_repositories(configs, "rhgo1749/not-managed")
    except intake.IntakeError as exc:
        assert "not managed and ready" in str(exc)
    else:
        raise AssertionError("unknown repository must fail closed")


def test_registry_fallback_skips_unready_and_reports_reason() -> None:
    snapshot = _snapshot(
        [
            _entry(
                "rhgo1749/ctrl-hangul",
                board="ctrlhangul",
                contract_paths=["AGENTS.md"],
            ),
            _entry(
                "rhgo1749/new-repo",
                ready=False,
                reason="checkout_missing",
            ),
        ]
    )

    configs, unready = intake._repository_configs_from_registry(
        snapshot,
        None,
    )

    assert [item.name for item in configs] == [
        "rhgo1749/ctrl-hangul"
    ]
    assert configs[0].board == "ctrlhangul"
    assert configs[0].default_branch == "main"
    assert configs[0].contract_paths == ("AGENTS.md",)
    assert unready == [
        {
            "repository": "rhgo1749/new-repo",
            "reason": "checkout_missing",
        }
    ]


def test_specific_unready_repository_fails_closed() -> None:
    snapshot = _snapshot(
        [
            _entry(
                "rhgo1749/new-repo",
                ready=False,
                reason="board_not_found_task_provenance",
            )
        ]
    )
    try:
        intake._repository_configs_from_registry(
            snapshot,
            "rhgo1749/new-repo",
        )
    except intake.IntakeError as exc:
        assert "repository is not ready" in str(exc)
        assert "board_not_found_task_provenance" in str(exc)
    else:
        raise AssertionError("specific unready repository must fail closed")


def test_registry_missing_repository_fails_closed() -> None:
    snapshot = _snapshot(
        [_entry("rhgo1749/ctrl-hangul")]
    )
    try:
        intake._repository_configs_from_registry(
            snapshot,
            "rhgo1749/not-managed",
        )
    except intake.IntakeError as exc:
        assert "not managed by registry" in str(exc)
    else:
        raise AssertionError("missing registry repository must fail closed")


def test_fixture_repository_config_is_offline_metadata() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "issues.json"
        path.write_text(
            json.dumps(
                {
                    "repository": "rhgo1749/project-x",
                    "repository_config": {
                        "board": "project-x",
                        "checkout": "/ws/projects/project-x",
                        "default_branch": "develop",
                        "contract_paths": [
                            "AGENTS.md",
                            ".agent/REQ_REQUEST_TEMPLATE.md",
                        ],
                    },
                    "issues": [],
                }
            ),
            encoding="utf-8",
        )

        configs = intake._fixture_repository_configs(path)
        assert len(configs) == 1
        assert configs[0].name == "rhgo1749/project-x"
        assert configs[0].default_branch == "develop"
        assert configs[0].contract_paths == (
            "AGENTS.md",
            ".agent/REQ_REQUEST_TEMPLATE.md",
        )


def test_default_branch_drives_repo_snapshot() -> None:
    with tempfile.TemporaryDirectory() as td:
        checkout = Path(td)
        config = _config(
            "rhgo1749/project-x",
            checkout=str(checkout),
            default_branch="develop",
            contract_paths=("AGENTS.md",),
        )
        calls: list[tuple[str, ...]] = []
        original = intake._run_git

        def fake_run_git(_checkout: str, *args: str):
            calls.append(args)
            if args == ("rev-parse", "--show-toplevel"):
                return 0, str(checkout), ""
            if args == ("remote", "get-url", "origin"):
                return (
                    0,
                    "https://github.com/rhgo1749/project-x.git",
                    "",
                )
            if args == (
                "rev-parse",
                "--verify",
                "origin/develop",
            ):
                return 0, "abc123", ""
            if args == (
                "cat-file",
                "-e",
                "origin/develop:AGENTS.md",
            ):
                return 0, "", ""
            return 1, "", "unexpected"

        try:
            intake._run_git = fake_run_git
            snapshot = intake._repo_snapshot(config)
        finally:
            intake._run_git = original

        assert snapshot.origin_sha == "abc123"
        assert (
            "rev-parse",
            "--verify",
            "origin/develop",
        ) in calls
        assert (
            "cat-file",
            "-e",
            "origin/develop:AGENTS.md",
        ) in calls


def test_task_body_uses_discovered_default_branch() -> None:
    config = _config(
        "rhgo1749/project-x",
        checkout="/ws/projects/project-x",
        default_branch="develop",
        contract_paths=("AGENTS.md",),
    )
    snapshot = intake.RepoSnapshot(
        origin_sha="abc123",
        remote="https://github.com/rhgo1749/project-x.git",
        contract_paths=("AGENTS.md",),
    )
    issue = {
        "number": 7,
        "title": "Example",
        "body": "Body",
        "labels": [{"name": "agent-ready"}],
        "html_url": "https://github.com/rhgo1749/project-x/issues/7",
    }

    body = intake._task_body(
        config,
        snapshot,
        issue,
        "github:rhgo1749/project-x:issue:7",
        "2026-08-12T00:00:00Z",
    )

    assert "origin/develop observed at import: abc123" in body
    assert "repository contract paths on origin/develop: AGENTS.md" in body
    assert "Target branch: `develop`" in body
    assert "current `origin/develop`" in body
    assert ".agent/PR_REQUEST_TEMPLATE.md" not in body
    assert ".agent/pr-requests/PR-NNN-<slug>.md" not in body


def test_board_lookup_uses_current_registry_scope() -> None:
    configs = (
        _config("rhgo1749/ctrl-hangul", board="ctrlhangul"),
    )
    assert (
        intake._board_for_repository(
            "rhgo1749/ctrl-hangul",
            configs,
        )
        == "ctrlhangul"
    )


def test_fixture_run_bypasses_github_and_live_registry() -> None:
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "issues.json"
        fixture.write_text(
            json.dumps(
                {
                    "repository": "rhgo1749/project-x",
                    "repository_config": {
                        "board": "project-x",
                        "checkout": "/ws/projects/project-x",
                        "default_branch": "develop",
                        "contract_paths": [],
                    },
                    "issues": [],
                }
            ),
            encoding="utf-8",
        )

        originals = {
            "_github_token": intake._github_token,
            "_load_registry_snapshot": intake._load_registry_snapshot,
        }

        def forbidden(*args, **kwargs):
            raise AssertionError("fixture mode touched live dependency")

        try:
            intake._github_token = forbidden
            intake._load_registry_snapshot = forbidden
            args = argparse.Namespace(
                dry_run=True,
                fixture_json=str(fixture),
                repository=None,
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                assert intake._run(args) == 0

            output = json.loads(stdout.getvalue())
            assert output["fixture"] is True
            assert output["repositories"] == [
                "rhgo1749/project-x"
            ]
            assert output["registry_unready"] == []
        finally:
            for name, value in originals.items():
                setattr(intake, name, value)


def test_live_run_threads_repository_scope_through_cleanup_and_sync() -> None:
    calls: dict[str, list[str]] = {
        "cleanup": [],
        "candidates": [],
        "sync": [],
    }

    snapshot = _snapshot(
        [
            _entry(
                "rhgo1749/ctrl-hangul",
                board="ctrlhangul",
            ),
            _entry("rhgo1749/re-bound"),
        ]
    )

    names = (
        "_github_token",
        "_load_registry_snapshot",
        "_telegram_config",
        "_run_closed_issue_cleanup",
        "_issue_candidates",
        "_sync_board",
    )
    originals = {name: getattr(intake, name) for name in names}

    try:
        intake._github_token = lambda: "token"
        intake._load_registry_snapshot = lambda token: snapshot
        intake._telegram_config = lambda: None

        def fake_cleanup(token, configs, *, dry_run):
            calls["cleanup"] = [item.name for item in configs]
            return []

        def fake_candidates(token, fixture_path, configs):
            calls["candidates"] = [item.name for item in configs]
            return []

        def fake_sync(config, token, *, dry_run=False):
            calls["sync"].append(config.name)
            return []

        intake._run_closed_issue_cleanup = fake_cleanup
        intake._issue_candidates = fake_candidates
        intake._sync_board = fake_sync

        args = argparse.Namespace(
            dry_run=True,
            fixture_json=None,
            repository="RHGO1749/CTRL-HANGUL",
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert intake._run(args) == 0

        output = json.loads(stdout.getvalue())
        expected = ["rhgo1749/ctrl-hangul"]

        assert calls["cleanup"] == expected
        assert calls["candidates"] == expected
        assert calls["sync"] == expected
        assert output["repositories"] == expected
        assert output["registry_unready"] == []
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_full_fallback_uses_ready_registry_and_reports_unready() -> None:
    calls: dict[str, list[str]] = {
        "cleanup": [],
        "sync": [],
    }

    snapshot = _snapshot(
        [
            _entry(
                "rhgo1749/ctrl-hangul",
                board="ctrlhangul",
            ),
            _entry("rhgo1749/re-bound"),
            _entry(
                "rhgo1749/new-repo",
                ready=False,
                reason="checkout_missing",
            ),
        ]
    )

    names = (
        "_github_token",
        "_load_registry_snapshot",
        "_claim_wake_scope",
        "_telegram_config",
        "_run_closed_issue_cleanup",
        "_issue_candidates",
        "_sync_board",
    )
    originals = {name: getattr(intake, name) for name in names}

    try:
        intake._github_token = lambda: "token"
        intake._load_registry_snapshot = lambda token: snapshot
        intake._claim_wake_scope = lambda: None
        intake._telegram_config = lambda: None

        def fake_cleanup(token, configs, *, dry_run):
            calls["cleanup"] = [item.name for item in configs]
            return []

        intake._run_closed_issue_cleanup = fake_cleanup
        intake._issue_candidates = (
            lambda token, fixture_path, configs: []
        )

        def fake_sync(config, token, *, dry_run=False):
            calls["sync"].append(config.name)
            return []

        intake._sync_board = fake_sync

        args = argparse.Namespace(
            dry_run=True,
            fixture_json=None,
            repository=None,
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert intake._run(args) == 0

        output = json.loads(stdout.getvalue())

        expected = [
            "rhgo1749/ctrl-hangul",
            "rhgo1749/re-bound",
        ]
        assert calls["cleanup"] == expected
        assert calls["sync"] == expected
        assert output["repositories"] == expected
        assert output["registry_unready"] == [
            {
                "repository": "rhgo1749/new-repo",
                "reason": "checkout_missing",
            }
        ]
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_registry_script_source_tree_fallback_exists() -> None:
    path = intake._registry_script_path()
    assert path.name == "repository_registry.py"
    assert path.is_file()



def test_event_router_claim_limits_live_run() -> None:
    calls: dict[str, list[str]] = {
        "cleanup": [],
        "sync": [],
    }
    snapshot = _snapshot(
        [
            _entry(
                "rhgo1749/ctrl-hangul",
                board="ctrlhangul",
            ),
            _entry("rhgo1749/re-bound"),
        ]
    )
    names = (
        "_github_token",
        "_load_registry_snapshot",
        "_claim_wake_scope",
        "_telegram_config",
        "_run_closed_issue_cleanup",
        "_issue_candidates",
        "_sync_board",
    )
    originals = {name: getattr(intake, name) for name in names}
    try:
        intake._github_token = lambda: "token"
        intake._load_registry_snapshot = lambda token: snapshot
        intake._claim_wake_scope = lambda: intake.WakeScope(
            mode="event",
            repositories=("rhgo1749/ctrl-hangul",),
            expires_at=9999999999,
        )
        intake._telegram_config = lambda: None

        def fake_cleanup(token, configs, *, dry_run):
            calls["cleanup"] = [config.name for config in configs]
            return []

        intake._run_closed_issue_cleanup = fake_cleanup
        intake._issue_candidates = lambda token, fixture_path, configs: []

        def fake_sync(config, token, *, dry_run=False):
            calls["sync"].append(config.name)
            return []

        intake._sync_board = fake_sync
        args = argparse.Namespace(
            dry_run=True,
            fixture_json=None,
            repository=None,
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert intake._run(args) == 0
        output = json.loads(stdout.getvalue())
        expected = ["rhgo1749/ctrl-hangul"]
        assert calls["cleanup"] == expected
        assert calls["sync"] == expected
        assert output["repositories"] == expected
        assert output["wake_scope"] == {
            "mode": "event",
            "repositories": expected,
        }
        assert output["scope_skipped"] == []
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(json.dumps({"ok": True, "tests": len(tests)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
