#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "automation" / "n8n" / "scripts" / "repository_registry.py"

spec = importlib.util.spec_from_file_location(
    "repository_registry_bootstrap_test",
    MODULE_PATH,
)
assert spec and spec.loader
registry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = registry
spec.loader.exec_module(registry)


def _create_board_db(root: Path, board: str, keys: list[str]) -> None:
    board_dir = root / board
    board_dir.mkdir(parents=True)
    con = sqlite3.connect(board_dir / "kanban.db")
    try:
        con.execute("CREATE TABLE tasks (idempotency_key TEXT)")
        con.executemany(
            "INSERT INTO tasks(idempotency_key) VALUES (?)",
            [(key,) for key in keys],
        )
        con.commit()
    finally:
        con.close()


def test_empty_canonical_board_bootstraps_first_intake() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(root, "h4v3-meowcore", [])
        evidence = registry._kanban_board_repository_evidence(root)
        assert registry._resolve_board("rhgo1749/H4V3-Meowcore", evidence) == (
            "h4v3-meowcore",
            "resolved_empty_canonical_board",
        )


def test_occupied_unmanaged_canonical_board_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(root, "h4v3-meowcore", ["manual:task"])
        evidence = registry._kanban_board_repository_evidence(root)
        board_evidence = evidence["h4v3-meowcore"]
        assert board_evidence.task_count == 1
        assert board_evidence.non_github_task_count == 1
        assert registry._resolve_board("rhgo1749/H4V3-Meowcore", evidence) == (
            None,
            "canonical_board_conflict",
        )


def test_noncanonical_empty_board_is_not_guessed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(root, "meowcore-manual", [])
        evidence = registry._kanban_board_repository_evidence(root)
        assert registry._resolve_board("rhgo1749/H4V3-Meowcore", evidence) == (
            None,
            "not_found_task_provenance",
        )


def test_canonical_board_with_other_repo_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(
            root,
            "h4v3-meowcore",
            ["github:rhgo1749/H4V3-DJ:issue:1"],
        )
        evidence = registry._kanban_board_repository_evidence(root)
        assert registry._resolve_board("rhgo1749/H4V3-Meowcore", evidence) == (
            None,
            "canonical_board_conflict",
        )


def test_durable_provenance_wins_after_bootstrap() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(
            root,
            "h4v3-meowcore",
            ["github:rhgo1749/H4V3-Meowcore:issue:2"],
        )
        evidence = registry._kanban_board_repository_evidence(root)
        assert registry._resolve_board("rhgo1749/H4V3-Meowcore", evidence) == (
            "h4v3-meowcore",
            "resolved_task_provenance",
        )


def test_legacy_noncanonical_board_still_resolves_from_provenance() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _create_board_db(
            root,
            "ctrlhangul",
            ["github:rhgo1749/ctrl-hangul:issue:51"],
        )
        evidence = registry._kanban_board_repository_evidence(root)
        assert registry._resolve_board("rhgo1749/ctrl-hangul", evidence) == (
            "ctrlhangul",
            "resolved_task_provenance",
        )


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS repository registry bootstrap suite ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
