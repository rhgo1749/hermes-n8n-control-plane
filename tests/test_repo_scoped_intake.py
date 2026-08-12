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

spec = importlib.util.spec_from_file_location("github_agent_ready_kanban_intake", MODULE_PATH)
assert spec and spec.loader
intake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)


def test_default_scope_keeps_full_fallback() -> None:
    assert intake._select_repositories(None) == intake.REPOSITORIES


def test_repository_scope_is_case_insensitive() -> None:
    selected = intake._select_repositories("RHGO1749/CTRL-HANGUL")
    assert len(selected) == 1
    assert selected[0].name == "rhgo1749/ctrl-hangul"
    assert selected[0].board == "ctrlhangul"


def test_unknown_repository_fails_closed() -> None:
    try:
        intake._select_repositories("rhgo1749/not-managed")
    except intake.IntakeError as exc:
        assert "repository is not configured" in str(exc)
    else:
        raise AssertionError("unknown repository must fail closed")


def test_fixture_cannot_escape_selected_repository_scope() -> None:
    selected = intake._select_repositories("rhgo1749/ctrl-hangul")

    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "issues.json"
        fixture.write_text(
            json.dumps(
                {
                    "repository": "rhgo1749/re-bound",
                    "issues": [
                        {
                            "number": 123,
                            "state": "open",
                            "labels": [{"name": "agent-ready"}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        try:
            intake._issue_candidates(None, fixture, selected)
        except intake.IntakeError as exc:
            assert "fixture repository is not configured" in str(exc)
        else:
            raise AssertionError("fixture outside selected scope must fail closed")


def test_run_threads_repository_scope_through_cleanup_and_sync() -> None:
    calls: dict[str, list[str]] = {
        "cleanup": [],
        "candidates": [],
        "sync": [],
    }

    originals = {
        name: getattr(intake, name)
        for name in (
            "_github_token",
            "_telegram_config",
            "_run_closed_issue_cleanup",
            "_issue_candidates",
            "_board_slugs",
            "_sync_board",
        )
    }

    try:
        intake._github_token = lambda: "token"
        intake._telegram_config = lambda: None

        def fake_cleanup(token, configs, *, dry_run):
            calls["cleanup"] = [config.name for config in configs]
            return []

        def fake_candidates(token, fixture_path, configs):
            calls["candidates"] = [config.name for config in configs]
            return []

        def fake_sync(config, token, *, dry_run=False):
            calls["sync"].append(config.name)
            return []

        intake._run_closed_issue_cleanup = fake_cleanup
        intake._issue_candidates = fake_candidates
        intake._board_slugs = lambda: {config.board for config in intake.REPOSITORIES}
        intake._sync_board = fake_sync

        args = argparse.Namespace(
            dry_run=False,
            fixture_json=None,
            repository="rhgo1749/ctrl-hangul",
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
