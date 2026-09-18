#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
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
    "github_agent_ready_kanban_intake_blocked_regression",
    MODULE_PATH,
)
assert spec and spec.loader
intake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)


def _config() -> object:
    return intake.RepositoryConfig(
        name="rhgo1749/project-x",
        board="project-x",
        checkout="/ws/projects/project-x",
        default_branch="main",
        contract_paths=("AGENTS.md",),
        display_name="project-x",
    )


def test_live_agent_ready_iterator_excludes_agent_blocked() -> None:
    original = intake._github_json

    def fake_github_json(token: str, path: str, params: dict[str, object]):
        assert token == "token"
        assert path == "/repos/rhgo1749/project-x/issues"
        assert params["state"] == "open"
        assert params["labels"] == "agent-ready"
        return (
            [
                {
                    "number": 1,
                    "state": "open",
                    "labels": [{"name": "agent-ready"}],
                },
                {
                    "number": 2,
                    "state": "open",
                    "labels": [
                        {"name": "agent-ready"},
                        {"name": "agent-blocked"},
                    ],
                },
            ],
            {},
        )

    try:
        intake._github_json = fake_github_json
        candidates = list(
            intake._iter_agent_ready_issues("token", "rhgo1749/project-x")
        )
    finally:
        intake._github_json = original

    assert [item["number"] for item in candidates] == [1]


def test_fixture_candidates_exclude_agent_blocked() -> None:
    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td) / "issues.json"
        fixture.write_text(
            json.dumps(
                [
                    {
                        "repository": "rhgo1749/project-x",
                        "number": 1,
                        "state": "open",
                        "labels": [{"name": "agent-ready"}],
                    },
                    {
                        "repository": "rhgo1749/project-x",
                        "number": 2,
                        "state": "open",
                        "labels": [
                            {"name": "agent-ready"},
                            {"name": "agent-blocked"},
                        ],
                    },
                ]
            ),
            encoding="utf-8",
        )

        candidates = intake._issue_candidates(None, fixture, (_config(),))

    assert [issue["number"] for _, issue in candidates] == [1]


if __name__ == "__main__":
    test_live_agent_ready_iterator_excludes_agent_blocked()
    test_fixture_candidates_exclude_agent_blocked()
    print("test_agent_blocked_intake.py: PASS")
