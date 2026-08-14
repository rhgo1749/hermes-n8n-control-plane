#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "automation" / "n8n" / "scripts" / "repository_registry.py"

spec = importlib.util.spec_from_file_location("repository_registry", MODULE_PATH)
assert spec and spec.loader
registry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = registry
spec.loader.exec_module(registry)


def _repo(
    full_name: str,
    repo_id: int = 1,
    branch: str = "main",
    *,
    contract_paths: list[str] | None = None,
) -> dict:
    owner, _ = full_name.split("/", 1)
    result = {
        "id": repo_id,
        "full_name": full_name,
        "default_branch": branch,
        "archived": False,
        "owner": {"login": owner},
    }
    if contract_paths is not None:
        result["contract_paths"] = contract_paths
    return result


def _create_board_db(root: Path, board: str, keys: list[str]) -> None:
    board_dir = root / board
    board_dir.mkdir(parents=True)
    con = sqlite3.connect(board_dir / "kanban.db")
    try:
        con.execute("CREATE TABLE tasks (idempotency_key TEXT)")
        con.executemany("INSERT INTO tasks(idempotency_key) VALUES (?)", [(key,) for key in keys])
        con.commit()
    finally:
        con.close()


def test_remote_normalization() -> None:
    expected = "rhgo1749/ctrl-hangul"
    for value in (
        "https://github.com/rhgo1749/ctrl-hangul.git",
        "git@github.com:rhgo1749/ctrl-hangul.git",
        "ssh://git@github.com/rhgo1749/ctrl-hangul.git",
        "git://github.com/rhgo1749/ctrl-hangul.git",
    ):
        assert registry._normalise_remote(value) == expected


def test_verified_checkout_and_resolved_board_is_ready() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checkout = root / "ctrl-hangul"
        checkout.mkdir(parents=True)

        entry = registry.build_entry(
            _repo("rhgo1749/ctrl-hangul", repo_id=42),
            root,
            contract_paths=registry.CONTRACT_CANDIDATES,
            board="ctrlhangul",
            board_status="resolved_task_provenance",
            origin_reader=lambda _: "git@github.com:rhgo1749/ctrl-hangul.git",
        )
        assert entry.repository_id == 42
        assert entry.checkout_status == "verified"
        assert entry.ready is True
        assert entry.reason is None
        assert entry.board == "ctrlhangul"
        assert entry.board_status == "resolved_task_provenance"
        assert entry.contract_paths == registry.CONTRACT_CANDIDATES


def test_verified_checkout_without_board_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "ctrl-hangul").mkdir()
        entry = registry.build_entry(
            _repo("rhgo1749/ctrl-hangul"),
            root,
            origin_reader=lambda _: "https://github.com/rhgo1749/ctrl-hangul.git",
        )
        assert entry.checkout_status == "verified"
        assert entry.ready is False
        assert entry.board is None
        assert entry.board_status == "not_found_task_provenance"
        assert entry.reason == "board_not_found_task_provenance"


def test_contract_detection_reads_default_branch_not_checkout() -> None:
    calls: list[tuple[str, str, dict, bool]] = []

    def fake_fetch(token: str, path: str, params: dict, *, allow_not_found: bool = False):
        calls.append((token, path, params, allow_not_found))
        if path.endswith("/contents/AGENTS.md"):
            return {"type": "file"}
        if path.endswith("/contents/.agent/REQ_REQUEST_TEMPLATE.md"):
            return {"type": "file"}
        return None

    contracts = registry._github_contracts(
        "secret",
        "rhgo1749/project-X",
        "develop",
        fetch_json=fake_fetch,
    )
    assert contracts == ("AGENTS.md", ".agent/REQ_REQUEST_TEMPLATE.md")
    assert len(calls) == len(registry.CONTRACT_CANDIDATES)
    assert all(call[2] == {"ref": "develop"} for call in calls)
    assert all(call[3] is True for call in calls)


def test_local_contract_drift_does_not_change_snapshot_contracts() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checkout = root / "ctrl-hangul"
        (checkout / "Docs").mkdir(parents=True)
        (checkout / "Docs" / "AGENTS.md").write_text("local-only", encoding="utf-8")

        snapshot = registry.registry_snapshot(
            [_repo("rhgo1749/ctrl-hangul", 1)],
            root,
            contract_reader=lambda repository, branch: (
                "AGENTS.md",
                ".agent/REQ_REQUEST_TEMPLATE.md",
            ),
            board_resolver=lambda repository: ("ctrlhangul", "resolved_task_provenance"),
            origin_reader=lambda _: "https://github.com/rhgo1749/ctrl-hangul.git",
        )
        entry = snapshot["repositories"][0]
        assert entry["contract_paths"] == [
            "AGENTS.md",
            ".agent/REQ_REQUEST_TEMPLATE.md",
        ]
        assert entry["board"] == "ctrlhangul"
        assert entry["ready"] is True


def test_missing_checkout_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        entry = registry.build_entry(
            _repo("rhgo1749/new-repo"),
            Path(td),
            board="new-repo",
            board_status="resolved_task_provenance",
        )
        assert entry.checkout_status == "missing"
        assert entry.ready is False
        assert entry.reason == "checkout_missing"
        assert entry.checkout.endswith("/new-repo")


def test_remote_mismatch_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "re-bound").mkdir()
        entry = registry.build_entry(
            _repo("rhgo1749/re-bound"),
            root,
            board="re-bound",
            board_status="resolved_task_provenance",
            origin_reader=lambda _: "https://github.com/rhgo1749/not-re-bound.git",
        )
        assert entry.checkout_status == "remote_mismatch"
        assert entry.ready is False
        assert entry.reason == "checkout_remote_mismatch"


def test_default_branch_is_repository_metadata() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "project-x").mkdir()
        entry = registry.build_entry(
            _repo("rhgo1749/project-X", branch="develop"),
            root,
            board="project-x",
            board_status="resolved_task_provenance",
            origin_reader=lambda _: "https://github.com/rhgo1749/project-X.git",
        )
        assert entry.default_branch == "develop"
        assert entry.canonical_slug == "project-x"


def test_snapshot_is_deterministic_and_has_no_repository_inventory() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name in ("z-repo", "a-repo"):
            (root / name).mkdir()
        snapshot = registry.registry_snapshot(
            [_repo("rhgo1749/z-repo", 2), _repo("rhgo1749/a-repo", 1)],
            root,
            contract_reader=lambda repository, branch: (),
            board_resolver=lambda repository: (None, "not_found_task_provenance"),
            origin_reader=lambda p: f"https://github.com/rhgo1749/{p.name}.git",
        )
        names = [item["repository"] for item in snapshot["repositories"]]
        assert names == ["rhgo1749/a-repo", "rhgo1749/z-repo"]
        assert snapshot["mode"] == "shadow"
        assert snapshot["schema_version"] == 2
        assert snapshot["board_authority"] == "tasks.idempotency_key"
        assert all(item["board"] is None for item in snapshot["repositories"])


def test_fixture_shape_and_contracts() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "repos.json"
        path.write_text(
            json.dumps(
                {
                    "items": [
                        _repo(
                            "rhgo1749/a-repo",
                            contract_paths=["AGENTS.md"],
                        )
                    ]
                }
            ),
            encoding="utf-8",
        )
        repos = registry._fixture_repositories(path)
        reader = registry._fixture_contract_reader(repos)
        assert len(repos) == 1
        assert repos[0]["full_name"] == "rhgo1749/a-repo"
        assert reader("rhgo1749/a-repo", "main") == ("AGENTS.md",)


def test_board_resolver_recovers_legacy_board_name_from_task_provenance() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(
            root,
            "ctrlhangul",
            [
                "github:rhgo1749/ctrl-hangul:issue:51",
                "github:rhgo1749/ctrl-hangul:issue:52",
                "not-a-github-key",
            ],
        )
        evidence = registry._kanban_board_repository_evidence(root)
        assert evidence == {"ctrlhangul": ("rhgo1749/ctrl-hangul",)}
        assert registry._resolve_board("rhgo1749/ctrl-hangul", evidence) == (
            "ctrlhangul",
            "resolved_task_provenance",
        )


def test_board_resolver_fails_closed_when_one_board_contains_multiple_repositories() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(
            root,
            "mixed-board",
            [
                "github:rhgo1749/ctrl-hangul:issue:1",
                "github:rhgo1749/re-bound:issue:2",
            ],
        )
        evidence = registry._kanban_board_repository_evidence(root)
        assert registry._resolve_board("rhgo1749/ctrl-hangul", evidence) == (
            None,
            "ambiguous_task_provenance",
        )


def test_board_resolver_fails_closed_when_repository_appears_on_multiple_boards() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(root, "board-a", ["github:rhgo1749/re-bound:issue:1"])
        _create_board_db(root, "board-b", ["github:rhgo1749/re-bound:issue:2"])
        evidence = registry._kanban_board_repository_evidence(root)
        assert registry._resolve_board("rhgo1749/re-bound", evidence) == (
            None,
            "ambiguous_multiple_boards",
        )


def test_archived_board_directory_is_ignored() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(root, "_archived", ["github:rhgo1749/ctrl-hangul:issue:1"])
        _create_board_db(root, "ctrlhangul", ["github:rhgo1749/ctrl-hangul:issue:2"])
        evidence = registry._kanban_board_repository_evidence(root)
        assert set(evidence) == {"ctrlhangul"}



def test_live_registry_snapshot_composes_existing_authorities() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checkout_root = root / "projects"
        kanban_root = root / "boards"
        (checkout_root / "ctrl-hangul").mkdir(parents=True)
        kanban_root.mkdir()

        originals = {
            "discover_repositories": registry.discover_repositories,
            "_kanban_board_repository_evidence": registry._kanban_board_repository_evidence,
            "_github_contracts": registry._github_contracts,
            "_git_origin": registry._git_origin,
        }

        try:
            registry.discover_repositories = (
                lambda token, owner, topic: [
                    _repo("rhgo1749/ctrl-hangul", 42)
                ]
            )
            registry._kanban_board_repository_evidence = (
                lambda root: {
                    "ctrlhangul": ("rhgo1749/ctrl-hangul",)
                }
            )
            registry._github_contracts = (
                lambda token, repository, branch: ("AGENTS.md",)
            )
            registry._git_origin = (
                lambda path: "https://github.com/rhgo1749/ctrl-hangul.git"
            )

            snapshot = registry.live_registry_snapshot(
                "token",
                "rhgo1749",
                "hermes-agent",
                checkout_root,
                kanban_root,
            )
        finally:
            for name, value in originals.items():
                setattr(registry, name, value)

        assert snapshot["schema_version"] == 2
        assert len(snapshot["repositories"]) == 1
        entry = snapshot["repositories"][0]
        assert entry["repository"] == "rhgo1749/ctrl-hangul"
        assert entry["board"] == "ctrlhangul"
        assert entry["default_branch"] == "main"
        assert entry["contract_paths"] == ["AGENTS.md"]
        assert entry["ready"] is True

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
