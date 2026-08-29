#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
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
        display_name=repository.split("/", 1)[1],
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
        "display_name": repository.split("/", 1)[1],
        "board": board or slug,
        "board_status": (
            "resolved_task_provenance"
            if ready
            else "not_found_task_provenance"
        ),
        "checkout": checkout or f"/ws/projects/{slug}",
        "checkout_status": "verified" if ready else "missing",
        "checkout_remote": f"https://github.com/{repository}.git",
        "contract_paths": (
            contract_paths if contract_paths is not None else ["AGENTS.md"]
        ),
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


def _strict_provisioning_stub(
    token: str,
    repositories,
    snapshot: dict,
    *,
    dry_run: bool,
):
    del token, snapshot, dry_run
    results = []
    seen = set()
    for repository in repositories:
        key = repository.casefold()
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "repository": repository,
                "checkout": f"/ws/projects/{repository.rsplit('/', 1)[1].casefold()}",
                "action": "reused",
            }
        )
    return results, [], False


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
        (checkout / "AGENTS.md").write_text("# contract\n", encoding="utf-8")
        config = _config(
            "rhgo1749/project-x",
            checkout=str(checkout),
            default_branch="develop",
            contract_paths=("AGENTS.md",),
        )
        calls: list[tuple[str, ...]] = []
        original = intake._run_git
        original_clean = intake._onboarding_checkout_is_clean

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
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            ):
                return 0, "develop", ""
            if args == (
                "rev-parse",
                "--verify",
                "origin/develop",
            ):
                return 0, "a" * 40, ""
            if args == (
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ):
                return 0, "a" * 40, ""
            if args == (
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignored=matching",
            ):
                return 0, "", ""
            if args == (
                "cat-file",
                "-e",
                "origin/develop:AGENTS.md",
            ):
                return 0, "", ""
            return 1, "", "unexpected"

        try:
            intake._run_git = fake_run_git
            intake.__dict__["_onboarding_checkout_is_clean"] = lambda checkout: True
            snapshot = intake._repo_snapshot(config)
        finally:
            intake._run_git = original
            intake.__dict__["_onboarding_checkout_is_clean"] = original_clean

        assert snapshot.origin_sha == "a" * 40
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
                        "contract_paths": ["AGENTS.md"],
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
        "_provision_scoped_checkouts",
    )
    originals = {name: getattr(intake, name) for name in names}

    try:
        intake._github_token = lambda: "token"
        intake._load_registry_snapshot = lambda token: snapshot
        intake.__dict__["_provision_scoped_checkouts"] = _strict_provisioning_stub
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
        "_provision_scoped_checkouts",
    )
    originals = {name: getattr(intake, name) for name in names}

    try:
        intake._github_token = lambda: "token"
        intake._load_registry_snapshot = lambda token: snapshot
        intake.__dict__["_provision_scoped_checkouts"] = _strict_provisioning_stub
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
        "_provision_scoped_checkouts",
    )
    originals = {name: getattr(intake, name) for name in names}
    try:
        intake._github_token = lambda: "token"
        intake._load_registry_snapshot = lambda token: snapshot
        intake.__dict__["_provision_scoped_checkouts"] = _strict_provisioning_stub
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


FAKE_REGISTRY_SNAPSHOT = {
    "schema_version": 2,
    "mode": "shadow",
    "board_authority": "tasks.idempotency_key",
    "repositories": [],
}


def test_override_boards_root_reaches_real_registry_subprocess() -> None:
    """HERMES_KANBAN_BOARDS_ROOT must reach the REAL registry subprocess.

    A recording stand-in registry executable captures its actual argv, so
    this fails deterministically if ``--kanban-root`` is built from anything
    other than the centralized ``_kanban_boards_root()`` resolver (e.g. the
    pre-round-3 hard-coded default home path).
    """
    with tempfile.TemporaryDirectory() as td:
        override_root = str(Path(td) / "custom-boards")
        recorder = Path(td) / "registry_argv.json"
        registry_script = Path(td) / "fake_registry.py"
        registry_script.write_text(
            "\n".join(
                [
                    "import json, sys",
                    "from pathlib import Path",
                    f"Path({str(recorder)!r}).write_text(json.dumps(sys.argv))",
                    f"print(json.dumps({FAKE_REGISTRY_SNAPSHOT!r}))",
                ]
            ),
            encoding="utf-8",
        )

        originals = {
            name: os.environ.get(name)
            for name in (
                "HERMES_KANBAN_BOARDS_ROOT",
                "HERMES_REPOSITORY_REGISTRY_SCRIPT",
                "HERMES_GITHUB_OWNER",
                "HERMES_GITHUB_TOPIC",
            )
        }
        try:
            os.environ["HERMES_KANBAN_BOARDS_ROOT"] = override_root
            os.environ["HERMES_REPOSITORY_REGISTRY_SCRIPT"] = str(registry_script)
            os.environ["HERMES_GITHUB_OWNER"] = "rhgo1749"
            os.environ["HERMES_GITHUB_TOPIC"] = "hermes-agent"

            snapshot = intake._load_registry_snapshot("token")

            assert snapshot == FAKE_REGISTRY_SNAPSHOT
            # The recording executable saw the REAL subprocess construction,
            # including --kanban-root resolved from the centralized resolver.
            argv = json.loads(recorder.read_text(encoding="utf-8"))
            assert "--kanban-root" in argv
            assert argv[argv.index("--kanban-root") + 1] == override_root
        finally:
            for name, value in originals.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_override_boards_root_honored_end_to_end_same_tick() -> None:
    """HERMES_KANBAN_BOARDS_ROOT drives registry discovery, ownership checks,
    the lease path, and the same-tick post-provision reload.

    A custom boards root must reach the bootstrap ownership DB probe (real
    reader, real sqlite row under the override root) and the migration/intake
    lease; after a board lands under that root the same-tick reload must
    resolve the ready entry so the first task is created in this tick. The
    real registry subprocess argv is covered separately by
    ``test_override_boards_root_reaches_real_registry_subprocess``.
    """
    with tempfile.TemporaryDirectory() as td:
        override_root = str(Path(td) / "custom-boards")
        ownership_roots: list[Path] = []
        existing_boards: set[str] = set()
        created_boards: list[str] = []
        tasks_created: list[str] = []

        originals = {
            name: getattr(intake, name)
            for name in (
                "_github_token",
                "_load_registry_snapshot",
                "_claim_wake_scope",
                "_telegram_config",
                "_run_closed_issue_cleanup",
                "_issue_candidates",
                "_repo_snapshot",
                "_create_task",
                "_sync_board",
                "_provision_scoped_checkouts",
                "_board_slugs",
                "_run_hermes",
                "_verify_bootstrap_checkout",
                "_strict_validate_bootstrap_checkout",
                "_board_repository_owners",
            )
        }

        def fake_load_registry_snapshot(token: str) -> dict:
            # Stands in for the real registry subprocess, which receives
            # ``--kanban-root <resolver()>`` (argv proven by the dedicated
            # subprocess regression above): its snapshot reflects board state
            # under the OVERRIDE root, not the default home. Before the
            # board exists under the override root it reports a bootstrap
            # intent; after provisioning, the same-tick reload resolves the
            # empty canonical board.
            if "brand-new" in existing_boards:
                return _snapshot([_entry("rhgo1749/brand-new")])
            return _snapshot(
                [
                    _bootstrap_entry(
                        "rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new"
                    )
                ]
            )

        def fake_board_repository_owners(board: str):
            db = intake._kanban_boards_root() / board / "kanban.db"
            ownership_roots.append(db.parent)
            return set()

        def fake_run_hermes(*args: str, **kwargs: Any) -> str:
            if "boards" in args and "create" in args:
                board = args[args.index("create") + 1]
                existing_boards.add(board)
                created_boards.append(board)
                return f"Board '{board}' created."
            raise AssertionError(f"unexpected hermes call: {args}")

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

        try:
            os.environ["HERMES_KANBAN_BOARDS_ROOT"] = override_root
            assert intake._kanban_boards_root() == Path(override_root)
            assert intake._intake_migration_lease_path() == (
                Path(override_root) / ".intake-migration.lock"
            )

            # Real ownership reader against a REAL board DB placed under the
            # override root: the resolver-derived path must be the one read.
            board_db_dir = Path(override_root) / "brand-new"
            board_db_dir.mkdir(parents=True, exist_ok=True)
            import sqlite3

            con = sqlite3.connect(board_db_dir / "kanban.db")
            con.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
                "status TEXT, idempotency_key TEXT)"
            )
            con.execute(
                "INSERT INTO tasks VALUES ('t_own', 'owned', 'done', "
                "'github:rhgo1749/brand-new:issue:9')"
            )
            con.commit()
            con.close()
            ownership = intake._board_repository_owners("brand-new")
            assert set(ownership) == {"rhgo1749/brand-new"}
            assert ownership.task_count == 1

            intake._load_registry_snapshot = fake_load_registry_snapshot
            intake.__dict__["_provision_scoped_checkouts"] = _strict_provisioning_stub
            intake._claim_wake_scope = lambda: None
            intake._telegram_config = lambda: None
            intake._run_closed_issue_cleanup = lambda token, configs, *, dry_run: []
            intake._board_slugs = lambda: set(existing_boards)
            intake._run_hermes = fake_run_hermes
            intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
            intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
            intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
            intake._board_repository_owners = fake_board_repository_owners
            intake._issue_candidates = lambda token, fixture_path, configs: [
                (configs[0], {"number": 1, "title": "First", "body": "", "labels": [{"name": "agent-ready"}]})
            ]
            intake._closing_merged_pr_numbers = lambda token, repository, issue_number: ()
            intake._repo_snapshot = lambda config: intake.RepoSnapshot(
                origin_sha="abc",
                remote="https://github.com/rhgo1749/brand-new.git",
                contract_paths=("AGENTS.md",),
            )
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
            # Same-tick reload happened under the override root.
            assert output["board_provisioning"] == [
                {
                    "repository": "rhgo1749/brand-new",
                    "board": "brand-new",
                    "action": "provisioned",
                }
            ]
            assert output["upserted_count"] == 1
            # Ownership probes (when an existing board candidate was checked)
            # resolved under the override root; the resolver itself honored it
            # for every read this tick performed.
            assert all(root == Path(override_root) for root in ownership_roots)
        finally:
            for name, value in originals.items():
                setattr(intake, name, value)
            if "HERMES_KANBAN_BOARDS_ROOT" in os.environ:
                del os.environ["HERMES_KANBAN_BOARDS_ROOT"]
            # Resolver back to the default home after the env override clears.
            assert str(intake._kanban_boards_root()).endswith("kanban/boards")


def test_provision_creates_missing_board_with_checkout_workdir() -> None:
    existing: set[str] = {"ctrlhangul"}
    created: list[str] = []

    def fake_run_hermes(*args: str, **kwargs: Any) -> str:
        board = args[args.index("create") + 1]
        created.append(board)
        existing.add(board)
        return f"Board '{board}' created."

    originals = {
        "_board_slugs": intake._board_slugs,
        "_run_hermes": intake._run_hermes,
        "_verify_bootstrap_checkout": intake._verify_bootstrap_checkout,
        "_strict_validate_bootstrap_checkout": intake._strict_validate_bootstrap_checkout,
        "_intake_mutation_lease": intake._intake_mutation_lease,
        "_board_repository_owners": intake._board_repository_owners,
    }
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = fake_run_hermes
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
        intake.__dict__["_board_repository_owners"] = lambda board: set()
        snapshot = _snapshot([_bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new")])
        report = intake._provision_bootstrap_boards(snapshot, dry_run=False, token="test-token")
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

    originals = {
        "_board_slugs": intake._board_slugs,
        "_run_hermes": intake._run_hermes,
        "_verify_bootstrap_checkout": intake._verify_bootstrap_checkout,
        "_strict_validate_bootstrap_checkout": intake._strict_validate_bootstrap_checkout,
        "_intake_mutation_lease": intake._intake_mutation_lease,
        "_board_repository_owners": intake._board_repository_owners,
    }
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = forbidden
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
        intake.__dict__["_board_repository_owners"] = lambda board: set()
        snapshot = _snapshot([_bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new")])
        assert intake._provision_bootstrap_boards(snapshot, dry_run=False, token="test-token") == []
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_provision_dry_run_never_mutates() -> None:
    existing: set[str] = {"ctrlhangul"}

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must never invoke the Hermes CLI")

    originals = {
        "_board_slugs": intake._board_slugs,
        "_run_hermes": intake._run_hermes,
        "_verify_bootstrap_checkout": intake._verify_bootstrap_checkout,
        "_strict_validate_bootstrap_checkout": intake._strict_validate_bootstrap_checkout,
        "_intake_mutation_lease": intake._intake_mutation_lease,
        "_board_repository_owners": intake._board_repository_owners,
    }
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = forbidden
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
        intake.__dict__["_board_repository_owners"] = lambda board: set()
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

    originals = {
        "_board_slugs": intake._board_slugs,
        "_run_hermes": intake._run_hermes,
        "_verify_bootstrap_checkout": intake._verify_bootstrap_checkout,
        "_strict_validate_bootstrap_checkout": intake._strict_validate_bootstrap_checkout,
        "_intake_mutation_lease": intake._intake_mutation_lease,
        "_board_repository_owners": intake._board_repository_owners,
    }
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = fake_run_hermes
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
        intake.__dict__["_board_repository_owners"] = lambda board: set()
        snapshot = _snapshot(
            [
                _bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new"),
                _bootstrap_entry("rhgo1749/other-new", "other-new", "/ws/projects/other-new"),
            ]
        )
        report = intake._provision_bootstrap_boards(
            snapshot,
            dry_run=False,
            token="test-token",
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

    originals = {
        "_board_slugs": intake._board_slugs,
        "_run_hermes": intake._run_hermes,
        "_verify_bootstrap_checkout": intake._verify_bootstrap_checkout,
        "_strict_validate_bootstrap_checkout": intake._strict_validate_bootstrap_checkout,
        "_intake_mutation_lease": intake._intake_mutation_lease,
        "_board_repository_owners": intake._board_repository_owners,
    }
    try:
        intake._board_slugs = lambda: set(existing)
        intake._run_hermes = fake_run_hermes
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
        intake.__dict__["_board_repository_owners"] = lambda board: set()
        snapshot = _snapshot([_bootstrap_entry("rhgo1749/brand-new", "brand-new", "/ws/projects/brand-new")])
        try:
            intake._provision_bootstrap_boards(snapshot, dry_run=False, token="test-token")
        except intake.IntakeError as exc:
            assert str(exc) == "board_provisioning_unavailable"
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
        assert str(exc) == "repository_metadata_invalid"
    else:
        raise AssertionError("malformed bootstrap intent must fail closed")


def test_provision_rejects_repository_derived_slug_mismatch() -> None:
    entry = _bootstrap_entry(
        "rhgo1749/brand-new",
        "wrong-slug",
        "/ws/projects/brand-new",
    )
    try:
        intake._provision_bootstrap_boards(_snapshot([entry]), dry_run=True)
    except intake.IntakeError as exc:
        assert str(exc) == "canonical_board_conflict"
    else:
        raise AssertionError("repository-derived slug mismatch must fail closed")


def test_provision_rejects_malformed_repository_before_create() -> None:
    entry = _bootstrap_entry(
        "rhgo1749/bad repo",
        "bad repo",
        "/ws/projects/bad-repo",
    )
    try:
        intake._provision_bootstrap_boards(_snapshot([entry]), dry_run=True)
    except intake.IntakeError as exc:
        assert "repository" in str(exc) and "invalid" in str(exc)
    else:
        raise AssertionError("malformed repository must fail closed")


def test_provision_rejects_unverified_checkout_before_create() -> None:
    entry = _bootstrap_entry(
        "rhgo1749/brand-new",
        "brand-new",
        "/definitely/missing/bootstrap-checkout",
    )
    try:
        intake._provision_bootstrap_boards(_snapshot([entry]), dry_run=True)
    except intake.IntakeError as exc:
        assert "checkout" in str(exc)
    else:
        raise AssertionError("missing checkout must fail closed")


def test_provision_rejects_foreign_existing_board_owner() -> None:
    existing = {"brand-new"}
    entry = _bootstrap_entry(
        "rhgo1749/brand-new",
        "brand-new",
        "/ws/projects/brand-new",
    )
    originals = {
        "_board_slugs": intake._board_slugs,
        "_board_repository_owners": intake._board_repository_owners,
        "_verify_bootstrap_checkout": intake._verify_bootstrap_checkout,
        "_strict_validate_bootstrap_checkout": intake._strict_validate_bootstrap_checkout,
        "_run_hermes": intake._run_hermes,
        "_intake_mutation_lease": intake._intake_mutation_lease,
    }
    try:
        intake.__dict__["_board_slugs"] = lambda: set(existing)
        intake.__dict__["_board_repository_owners"] = lambda board: {
            "rhgo1749/other-repo"
        }
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_run_hermes"] = lambda *args, **kwargs: (
            (_ for _ in ()).throw(AssertionError("foreign owner must block create"))
        )
        intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
        try:
            intake._provision_bootstrap_boards(_snapshot([entry]), dry_run=False, token="test-token")
        except intake.IntakeError as exc:
            assert str(exc) == "canonical_board_conflict"
        else:
            raise AssertionError("foreign board ownership must fail closed")
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_provision_rejects_occupied_unmanaged_existing_board() -> None:
    entry = _bootstrap_entry(
        "rhgo1749/brand-new",
        "brand-new",
        "/ws/projects/brand-new",
    )
    originals = {
        "_board_slugs": intake._board_slugs,
        "_board_repository_owners": intake._board_repository_owners,
        "_verify_bootstrap_checkout": intake._verify_bootstrap_checkout,
        "_strict_validate_bootstrap_checkout": intake._strict_validate_bootstrap_checkout,
        "_run_hermes": intake._run_hermes,
    }
    try:
        intake.__dict__["_board_slugs"] = lambda: {"brand-new"}
        intake.__dict__["_board_repository_owners"] = lambda board: intake.BoardOwnership(
            task_count=1,
            non_github_task_count=1,
        )
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_run_hermes"] = lambda *args, **kwargs: (
            (_ for _ in ()).throw(AssertionError("occupied board must not create"))
        )
        try:
            intake._provision_bootstrap_boards(_snapshot([entry]), dry_run=True)
        except intake.IntakeError as exc:
            assert str(exc) == "canonical_board_conflict"
        else:
            raise AssertionError("occupied unmanaged board must fail closed")
    finally:
        for name, value in originals.items():
            setattr(intake, name, value)


def test_provision_without_intents_makes_no_board_calls() -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no bootstrap intent, no Hermes CLI calls")

    originals = {
        "_board_slugs": intake._board_slugs,
        "_run_hermes": intake._run_hermes,
        "_verify_bootstrap_checkout": intake._verify_bootstrap_checkout,
        "_strict_validate_bootstrap_checkout": intake._strict_validate_bootstrap_checkout,
        "_intake_mutation_lease": intake._intake_mutation_lease,
        "_board_repository_owners": intake._board_repository_owners,
    }
    try:
        intake._board_slugs = forbidden
        intake._run_hermes = forbidden
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
        intake.__dict__["_board_repository_owners"] = lambda board: set()
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
        "_provision_scoped_checkouts",
        "_board_slugs",
        "_run_hermes",
        "_verify_bootstrap_checkout",
        "_strict_validate_bootstrap_checkout",
        "_intake_mutation_lease",
        "_board_repository_owners",
    )
    originals = {name: getattr(intake, name) for name in names}

    try:
        intake._github_token = lambda: "token"
        intake._load_registry_snapshot = fake_load_registry_snapshot
        intake.__dict__["_provision_scoped_checkouts"] = _strict_provisioning_stub
        intake._claim_wake_scope = lambda: None
        intake._telegram_config = lambda: None
        intake._run_closed_issue_cleanup = lambda token, configs, *, dry_run: []
        intake._board_slugs = fake_board_slugs
        intake._run_hermes = fake_run_hermes
        intake.__dict__["_verify_bootstrap_checkout"] = lambda repository, checkout: Path(checkout)
        intake.__dict__["_strict_validate_bootstrap_checkout"] = lambda token, repository, checkout: Path(checkout)
        intake.__dict__["_intake_mutation_lease"] = lambda: contextlib.nullcontext()
        intake.__dict__["_board_repository_owners"] = lambda board: set()
        intake._issue_candidates = lambda token, fixture_path, configs: [(configs[0], issue)]
        intake._closing_merged_pr_numbers = lambda token, repository, issue_number: ()
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
