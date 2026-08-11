#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
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


def _repo(full_name: str, repo_id: int = 1, branch: str = "main") -> dict:
    owner, _ = full_name.split("/", 1)
    return {
        "id": repo_id,
        "full_name": full_name,
        "default_branch": branch,
        "archived": False,
        "owner": {"login": owner},
    }


def test_remote_normalization() -> None:
    expected = "rhgo1749/ctrl-hangul"
    for value in (
        "https://github.com/rhgo1749/ctrl-hangul.git",
        "git@github.com:rhgo1749/ctrl-hangul.git",
        "ssh://git@github.com/rhgo1749/ctrl-hangul.git",
        "git://github.com/rhgo1749/ctrl-hangul.git",
    ):
        assert registry._normalise_remote(value) == expected


def test_verified_checkout_and_contract_detection_stays_shadow_unready() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        checkout = root / "ctrl-hangul"
        (checkout / "Docs").mkdir(parents=True)
        (checkout / ".agent").mkdir(parents=True)
        (checkout / "AGENTS.md").write_text("x", encoding="utf-8")
        (checkout / "AGENTS_PROJECT.md").write_text("x", encoding="utf-8")
        (checkout / "Docs" / "AGENTS.md").write_text("x", encoding="utf-8")
        (checkout / ".agent" / "PR_REQUEST_TEMPLATE.md").write_text("x", encoding="utf-8")

        entry = registry.build_entry(
            _repo("rhgo1749/ctrl-hangul", repo_id=42),
            root,
            origin_reader=lambda _: "git@github.com:rhgo1749/ctrl-hangul.git",
        )
        assert entry.repository_id == 42
        assert entry.checkout_status == "verified"
        assert entry.ready is False
        assert entry.reason == "board_unresolved_shadow_phase"
        assert entry.board is None
        assert entry.board_status == "unresolved_shadow_phase"
        assert entry.contract_paths == registry.CONTRACT_CANDIDATES


def test_missing_checkout_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        entry = registry.build_entry(_repo("rhgo1749/new-repo"), Path(td))
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
            origin_reader=lambda p: f"https://github.com/rhgo1749/{p.name}.git",
        )
        names = [item["repository"] for item in snapshot["repositories"]]
        assert names == ["rhgo1749/a-repo", "rhgo1749/z-repo"]
        assert snapshot["mode"] == "shadow"
        assert snapshot["schema_version"] == 1
        assert all(item["board"] is None for item in snapshot["repositories"])


def test_fixture_shape() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "repos.json"
        path.write_text(json.dumps({"items": [_repo("rhgo1749/a-repo")]}), encoding="utf-8")
        repos = registry._fixture_repositories(path)
        assert len(repos) == 1
        assert repos[0]["full_name"] == "rhgo1749/a-repo"


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(json.dumps({"ok": True, "tests": len(tests)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
