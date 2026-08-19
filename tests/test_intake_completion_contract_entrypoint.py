#!/usr/bin/env python3
"""Regression checks for the GitHub intake lifecycle-contract wrapper."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = (
    ROOT
    / "automation"
    / "hermes"
    / "scripts"
    / "github-agent-ready-kanban-intake-entrypoint.py"
)

spec = importlib.util.spec_from_file_location("github_intake_entrypoint_test", ENTRYPOINT)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def _render_body() -> str:
    config = mod.RepositoryConfig(
        name="rhgo1749/example",
        board="example",
        checkout="/ws/projects/example",
        default_branch="main",
        contract_paths=("AGENTS.md",),
    )
    snapshot = mod.RepoSnapshot(
        origin_sha="0123456789abcdef",
        remote="https://github.com/rhgo1749/example.git",
        contract_paths=("AGENTS.md",),
    )
    issue = {
        "number": 42,
        "title": "Example issue",
        "body": "Implement the example.",
        "html_url": "https://github.com/rhgo1749/example/issues/42",
        "labels": [{"name": "agent-ready"}],
    }
    return mod._task_body(
        config,
        snapshot,
        issue,
        "github:rhgo1749/example:issue:42",
        "2026-08-19T00:00:00Z",
    )


def test_worker_completion_uses_core_terminal_action() -> None:
    body = _render_body()
    assert "must finish the worker run with core `kanban_complete`" in body
    assert "Do not call `kanban_request_review`" in body
    assert "core `done` transition is provisional" in body
    assert "projects the card to parked `review`" in body
    assert "Authoritative `done` requires a fresh GitHub API read" in body


def test_external_wait_never_keeps_worker_running() -> None:
    body = _render_body()
    assert "may remain RUNNING solely to wait for future GitHub Actions/checks" in body
    assert "never use `sleep` or repeated polling" in body
    assert "Pending future CI/checks, human review, merge, or comments" in body
    assert "Future PR lifecycle belongs to GitHub + edge reconciliation" in body


def test_main_and_controller_ownership_are_separated() -> None:
    body = _render_body()
    assert "## Kanban lead orchestration contract" in body
    assert "Main is the planner/router/judge, not the default implementer" in body
    assert "Encode real dependencies before downstream work runs" in body
    assert "Deterministic Controller/edge logic owns event intake" in body
    assert "Do not recreate controller behavior through agent reasoning loops" in body
    assert "## Luna lead execution contract" not in body


def test_legacy_review_handoff_contract_is_removed() -> None:
    body = _render_body()
    assert "status must be `review`, never `done`" not in body
    assert "Worker implementation completion is a review handoff" not in body
    assert "completion contract: github-pr" in body


def test_contract_drift_fails_closed() -> None:
    fake = ModuleType("fake_intake")
    fake._task_body = lambda *args, **kwargs: "unexpected contract"
    mod._install_completion_contract_overlay(fake)
    try:
        fake._task_body()
    except RuntimeError as exc:
        assert "completion contract drifted" in str(exc)
    else:
        raise AssertionError("contract drift must fail closed")


def test_lead_contract_drift_fails_closed() -> None:
    fake = ModuleType("fake_intake_lead")
    fake._task_body = lambda *args, **kwargs: mod._OLD_COMPLETION_CONTRACT
    mod._install_completion_contract_overlay(fake)
    try:
        fake._task_body()
    except RuntimeError as exc:
        assert "Kanban lead contract drifted" in str(exc)
    else:
        raise AssertionError("lead contract drift must fail closed")


if __name__ == "__main__":
    tests = (
        test_worker_completion_uses_core_terminal_action,
        test_external_wait_never_keeps_worker_running,
        test_main_and_controller_ownership_are_separated,
        test_legacy_review_handoff_contract_is_removed,
        test_contract_drift_fails_closed,
        test_lead_contract_drift_fails_closed,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
