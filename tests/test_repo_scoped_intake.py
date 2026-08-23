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
from typing import Any


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




def _bootstrap_entry(repository: str, board: str, checkout: str) -> dict:
    entry = _entry(repository, ready=False, reason="board_not_found_task_provenance")
    entry["bootstrap"] = {"board": board, "checkout": checkout}
    return entry


def test_provision_creates_missing_board_with_checkout_workdir() -> None:
    existing: set[str] = {"ctrlhangul"}
    created: list[str] = []

    def fake_run_hermes(*args: str, **kwargs: Any) -> str:
        board = args[args.index("create") + 1]
        created.append(board)
        existing.add(board)
        return f"Board '{board}' created."

    originals = {"_board_slugs": intake._board_slugs, "_run_hermes": intake._run_hermes}
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = fake_run_hermes
        snapshot = _snapshot([_bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new")])
        report = intake._provision_bootstrap_boards(snapshot, dry_run=False)
        assert report == [
            {"repository": "rhgo1749/brand-new", "board": "brand-new", "action": "provisioned"}
        ]
        assert created == ["brand-new"]
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_provision_skips_existing_board_idempotently() -> None:
    existing: set[str] = {"brand-new", "ctrlhangul"}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not create an already-existing board")

    originals = {"_board_slugs": intake._board_slugs, "_run_hermes": intake._run_hermes}
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = forbidden
        snapshot = _snapshot([_bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new")])
        assert intake._provision_bootstrap_boards(snapshot, dry_run=False) == []
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_provision_dry_run_never_mutates() -> None:
    existing: set[str] = {"ctrlhangul"}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must never invoke the Hermes CLI")

    originals = {"_board_slugs": intake._board_slugs, "_run_hermes": intake._run_hermes}
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = forbidden
        snapshot = _snapshot([_bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new")])
        report = intake._provision_bootstrap_boards(snapshot, dry_run=True)
        assert report == [
            {"repository": "rhgo1749/brand-new", "board": "brand-new", "action": "would-provision"}
        ]
        assert existing == {"ctrlhangul"}
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_provision_scope_restricts_provisioning() -> None:
    existing: set[str] = {"ctrlhangul"}
    created: list[str] = []

    def fake_run_hermes(*args: str, **kwargs: Any) -> str:
        board = args[args.index("create") + 1]
        created.append(board)
        existing.add(board)
        return f"Board '{board}' created."

    originals = {"_board_slugs": intake._board_slugs, "_run_hermes": intake._run_hermes}
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = fake_run_hermes
        snapshot = _snapshot(
            [
                _bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new"),
                _bootstrap_entry("rhgo1749/other-new", "other-new", "/ws/projects/other-new"),
            ]
        )
        report = intake._provision_bootstrap_boards(
            snapshot,
            dry_run=False,
            scope=("RHGO1749/BRAND-NEW",),
        )
        assert [item["board"] for item in report] == ["brand-new"]
        assert created == ["brand-new"]
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_provision_fails_closed_if_board_does_not_land() -> None:
    existing: set[str] = {"ctrlhangul"}

    def fake_run_hermes(*args: str, **kwargs: Any) -> str:
        return "Board created."  # claims success but the board never lands

    originals = {"_board_slugs": intake._board_slugs, "_run_hermes": intake._run_hermes}
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = fake_run_hermes
        snapshot = _snapshot([_bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new")])
        try:
            intake._provision_bootstrap_boards(snapshot, dry_run=False)
        except intake.IntakeError as exc:
            assert "did not land" in str(exc)
        else:
            raise AssertionError("provisioning that did not land must fail closed")
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_provision_malformed_intent_fails_closed() -> None:
    entry = _entry("rhgo1749/brand-new", ready=False, reason="board_not_found_task_provenance")
    entry["bootstrap"] = {"board": "brand-new"}  # checkout missing
    snapshot = _snapshot([entry])
    try:
        intake._provision_bootstrap_boards(snapshot, dry_run=True)
    except intake.IntakeError as exc:
        assert "malformed" in str(exc)
    else:
        raise AssertionError("malformed bootstrap intent must fail closed")


def test_provision_without_intents_makes_no_board_calls() -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no bootstrap intent, no Hermes CLI calls")

    originals = {"_board_slugs": intake._board_slugs, "_run_hermes": intake._run_hermes}
    try:
        intake._board_slugs = forbidden
        intake._run_hermes = forbidden
        snapshot = _snapshot([_entry("rhgo1749/ctrl-hangul")])
        assert intake._provision_bootstrap_boards(snapshot, dry_run=True) == []
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_live_run_provisions_then_intakes_first_task_same_tick() -> None:
    """End-to-end: a new opted-in repository with no board gets its canonical
    board provisioned and its first agent-ready Issue imported in the SAME
    tick (the just-created empty canonical board resolves via the
    empty-canonical-board rule after the same-tick snapshot reload)."""
    existing_boards: set[str] = {"ctrlhangul"}
    created_boards: list[str] = []
    tasks_created: list[str] = []

    def fake_board_slugs() -> set[str]:
        return set(existing_boards)

    def fake_run_hermes(*args: str, **kwargs: Any) -> str:
        if "boards" in args and "create" in args:
            board = args[args.index("create") + 1]
            existing_boards.add(board)
            created_boards.append(board)
            return f"Board '{board}' created."
        raise AssertionError(f"unexpected hermes call: {args}")

    def fake_load_registry_snapshot(token: str) -> dict:
        # First load: no board, bootstrap intent present. After the board is
        # provisioned, the same-tick reload resolves the empty canonical
        # board and returns a ready entry.
        if "brand-new" in existing_boards:
            return _snapshot([_entry("rhgo1749/brand-new")])
        return _snapshot(
            [_bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new")]
        )

    issue = {
        "number": 1,
        "title": "First",
        "body": "",
        "labels": [{"name": "agent-ready"}],
    }

    names = (
        "_github_token",
        "_load_registry_snapshot",
        "_claim_wake_scope",
        "_telegram_config",
        "_run_closed_issue_cleanup",
        "_issue_candidates",
        "_repo_snapshot",
        "_create_task",
        "_sync_board",
        "_board_slugs",
        "_run_hermes",
    )
    originals = {name: getattr(intake, name) for name in names}

    try:
        intake._github_token = lambda: "token"
        intake._load_registry_snapshot = fake_load_registry_snapshot
        intake._claim_wake_scope = lambda: None
        intake._telegram_config = lambda: None
        intake._run_closed_issue_cleanup = lambda token, configs, *, dry_run: []
        intake._board_slugs = fake_board_slugs
        intake._run_hermes = fake_run_hermes
        intake._issue_candidates = lambda token, fixture_path, configs: [(configs[0], issue)]
        intake._merged_linked_pr_numbers = lambda token, repository, issue_number: ()
        intake._repo_snapshot = lambda config: intake.RepoSnapshot(
            origin_sha="abc",
            remote="https://github.com/rhgo1749/brand-new.git",
            contract_paths=("AGENTS.md",),
        )

        def fake_create_task(config, issue_arg, snapshot, imported_at, *, tick_started):
            tasks_created.append(config.board)
            return {
                "key": f"github:{config.name}:issue:1",
                "board": config.board,
                "task_id": "t_test",
                "status": "ready",
                "created": True,
                "issue_number": 1,
            }

        intake._create_task = fake_create_task
        intake._sync_board = lambda config, token, *, dry_run=False: []

        args = argparse.Namespace(
            dry_run=False,
            fixture_json=None,
            repository="rhgo1749/brand-new",
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            assert intake._run(args) == 0

        output = json.loads(stdout.getvalue())
        assert created_boards == ["brand-new"]
        assert tasks_created == ["brand-new"]
        assert output["board_provisioning"] == [
            {
                "repository": "rhgo1749/brand-new",
                "board": "brand-new",
                "action": "provisioned",
            }
        ]
        assert output["upserted_count"] == 1
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
