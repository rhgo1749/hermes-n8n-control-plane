#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "automation" / "n8n" / "scripts" / "repository_registry.py"

spec = importlib.util.spec_from_file_location(
    "repository_registry_board_workdir_test",
    MODULE_PATH,
)
assert spec and spec.loader
registry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = registry
spec.loader.exec_module(registry)


def _repo(full_name: str = "rhgo1749/H4V3-Meowcore") -> dict:
    owner, _ = full_name.split("/", 1)
    return {
        "id": 1332961604,
        "full_name": full_name,
        "default_branch": "main",
        "archived": False,
        "disabled": False,
        "owner": {"login": owner},
    }


def _write_board(
    boards_root: Path,
    board: str,
    *,
    default_workdir: str | None,
    slug: str | None = None,
) -> None:
    board_dir = boards_root / board
    board_dir.mkdir(parents=True)
    payload = {"slug": slug or board}
    if default_workdir is not None:
        payload["default_workdir"] = default_workdir
    (board_dir / "board.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_resolved_board_default_workdir_wins_over_lowercase_slug() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checkout_root = root / "projects"
        boards_root = root / "boards"
        checkout = checkout_root / "H4V3-Meowcore"
        checkout.mkdir(parents=True)
        _write_board(
            boards_root,
            "h4v3-meowcore",
            default_workdir=str(checkout),
        )

        entry = registry.build_entry(
            _repo(),
            checkout_root,
            board="h4v3-meowcore",
            board_status="resolved_task_provenance",
            checkout_path=registry._board_default_workdir(
                boards_root,
                "h4v3-meowcore",
            ),
            origin_reader=lambda path: (
                "https://github.com/rhgo1749/H4V3-Meowcore.git"
                if path == checkout
                else None
            ),
        )

        assert entry.checkout == str(checkout)
        assert entry.checkout_status == "verified"
        assert entry.ready is True


def test_missing_board_metadata_keeps_slug_fallback() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checkout_root = root / "projects"
        boards_root = root / "boards"
        checkout = checkout_root / "h4v3-meowcore"
        checkout.mkdir(parents=True)
        (boards_root / "h4v3-meowcore").mkdir(parents=True)

        entry = registry.build_entry(
            _repo(),
            checkout_root,
            board="h4v3-meowcore",
            board_status="resolved_task_provenance",
            checkout_path=registry._board_default_workdir(
                boards_root,
                "h4v3-meowcore",
            ),
            origin_reader=lambda path: (
                "https://github.com/rhgo1749/H4V3-Meowcore.git"
                if path == checkout
                else None
            ),
        )

        assert entry.checkout == str(checkout)
        assert entry.checkout_status == "verified"
        assert entry.ready is True


def test_board_workdir_still_requires_matching_repository_remote() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checkout_root = root / "projects"
        boards_root = root / "boards"
        checkout = checkout_root / "H4V3-Meowcore"
        checkout.mkdir(parents=True)
        _write_board(
            boards_root,
            "h4v3-meowcore",
            default_workdir=str(checkout),
        )

        entry = registry.build_entry(
            _repo(),
            checkout_root,
            board="h4v3-meowcore",
            board_status="resolved_task_provenance",
            checkout_path=registry._board_default_workdir(
                boards_root,
                "h4v3-meowcore",
            ),
            origin_reader=lambda _: "https://github.com/rhgo1749/H4V3-DJ.git",
        )

        assert entry.checkout_status == "remote_mismatch"
        assert entry.ready is False
        assert entry.reason == "checkout_remote_mismatch"


def test_relative_board_workdir_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        boards_root = Path(td) / "boards"
        _write_board(
            boards_root,
            "h4v3-meowcore",
            default_workdir="../H4V3-Meowcore",
        )

        try:
            registry._board_default_workdir(boards_root, "h4v3-meowcore")
        except registry.RegistryError as exc:
            assert "must be absolute" in str(exc)
        else:
            raise AssertionError("relative default_workdir must fail closed")


def test_board_metadata_slug_mismatch_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        boards_root = root / "boards"
        _write_board(
            boards_root,
            "h4v3-meowcore",
            default_workdir=str(root / "projects" / "H4V3-Meowcore"),
            slug="other-board",
        )

        try:
            registry._board_default_workdir(boards_root, "h4v3-meowcore")
        except registry.RegistryError as exc:
            assert "slug mismatch" in str(exc)
        else:
            raise AssertionError("metadata slug mismatch must fail closed")


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS repository registry board workdir suite ({len(tests)} tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
