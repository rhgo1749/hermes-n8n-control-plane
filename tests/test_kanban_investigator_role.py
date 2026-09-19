#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOYER_PATH = ROOT / "automation/hermes/scripts/deploy-kanban-investigator-profile-contracts.py"
GUARD_PATH = ROOT / "automation/hermes/scripts/kanban-specialist-completion-guard.py"
CONTRACT_ROOT = ROOT / "automation/hermes/profile-contracts"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _profile_tree(home: Path, *, include_investigator: bool = True) -> dict[str, str]:
    originals = {
        "kanban-main": "MAIN ORIGINAL\n",
        "kanban-developer": "DEVELOPER ORIGINAL\n",
        "kanban-reviewer": "REVIEWER ORIGINAL\n",
        "kanban-designer": "DESIGNER ORIGINAL\n",
    }
    if include_investigator:
        originals["kanban-investigator"] = "COPIED REVIEWER SOUL\n"
    for profile, soul in originals.items():
        profile_dir = home / "profiles" / profile
        profile_dir.mkdir(parents=True)
        (profile_dir / "SOUL.md").write_text(soul, encoding="utf-8")
    return originals


def test_investigator_is_a_local_only_specialist() -> None:
    guard = _load("investigator_completion_guard", GUARD_PATH)

    blocked = guard.evaluate_payload(
        {
            "tool_name": "kanban_create",
            "tool_input": {
                "title": "investigate regression",
                "assignee": "kanban-investigator",
                "completion_contract": "rhgo1749/ctrl-hangul",
            },
        }
    )
    assert blocked == 2

    allowed = guard.evaluate_payload(
        {
            "tool_name": "kanban_create",
            "tool_input": {
                "title": "investigate regression",
                "assignee": "kanban-investigator",
                "completion_contract": "local-only",
                "max_runtime_seconds": 7200,
            },
        }
    )
    assert allowed == 0


def test_profile_deployer_preserves_existing_souls_and_replaces_investigator() -> None:
    deployer = _load("investigator_profile_deployer", DEPLOYER_PATH)
    with tempfile.TemporaryDirectory(prefix="investigator-profile-deploy-") as directory:
        home = Path(directory) / ".hermes"
        originals = _profile_tree(home)

        first = deployer.deploy(home)
        assert any(plan.changed for plan in first)

        for profile in (
            "kanban-main",
            "kanban-developer",
            "kanban-reviewer",
            "kanban-designer",
        ):
            soul = (home / "profiles" / profile / "SOUL.md").read_text(encoding="utf-8")
            assert originals[profile].strip() in soul
            assert soul.count(deployer.MARKER_BEGIN) == 1
            assert soul.count(deployer.MARKER_END) == 1
            assert "H4V3" in soul

        main_soul = (home / "profiles/kanban-main/SOUL.md").read_text(encoding="utf-8")
        assert "### Edge-admitted rework boundary" in main_soul
        assert "edge has already decided rework admission for that round" in main_soul
        assert "do not block solely because `AGENT_REWORK_RETRY` is absent" in main_soul
        assert "fresh trusted PR-side `agent-rework` command intentionally opens a label-origin round" in main_soul
        assert "`AGENT_REWORK_RETRY` is a different one-shot recovery signal" in main_soul
        assert "### Specialist workspace creation" in main_soul
        assert 'workspace_kind="worktree"' in main_soul
        assert "stable `idempotency_key`" in main_soul
        assert "Omit `workspace_path`, `branch`, and `branch_name`" in main_soul
        assert "Do not fall back to `hermes kanban create`, `git worktree add`" in main_soul
        assert "### Dependency waiting state" in main_soul
        assert "Do not use `initial_status=blocked` merely because a parent is still open" in main_soul
        assert "Reviewer todo (parent=Developer)" in main_soul
        assert "### GitHub-backed root join" in main_soul
        assert "direct parent of the intake root" in main_soul
        assert "fresh-read the root dependency graph" in main_soul
        assert "do not call root `kanban_complete`" in main_soul
        assert "### Slow-local worker runtime and retry policy" in main_soul
        assert "1800 seconds (30" not in main_soul
        assert "7200 seconds (120" in main_soul
        assert "10800 seconds (180" in main_soul
        assert "max_retries=5" in main_soul

        investigator = (home / "profiles/kanban-investigator/SOUL.md").read_text(encoding="utf-8")
        expected = (CONTRACT_ROOT / "kanban-investigator-SOUL.md").read_text(encoding="utf-8").rstrip() + "\n"
        assert investigator == expected
        assert "COPIED REVIEWER SOUL" not in investigator
        assert "You are Hermes Kanban Investigator." in investigator

        second = deployer.deploy(home)
        assert all(not plan.changed for plan in second)
        for profile in originals:
            backups = list((home / "profiles" / profile / ".h4v3-backups").glob("investigator-contract-*/SOUL.md"))
            assert len(backups) == 1


def test_profile_deployer_updates_managed_block_idempotently() -> None:
    deployer = _load("investigator_profile_deployer_update", DEPLOYER_PATH)
    with tempfile.TemporaryDirectory(prefix="investigator-profile-update-") as directory:
        home = Path(directory) / ".hermes"
        _profile_tree(home)
        deployer.deploy(home)

        main = home / "profiles/kanban-main/SOUL.md"
        content = main.read_text(encoding="utf-8")
        content = content.replace("When operating as `kanban-main`", "STALE CONTRACT")
        main.write_text(content, encoding="utf-8")

        deployer.deploy(home)
        refreshed = main.read_text(encoding="utf-8")
        assert "STALE CONTRACT" not in refreshed
        assert "When operating as `kanban-main`" in refreshed
        assert "edge has already decided rework admission for that round" in refreshed
        assert "do not block solely because `AGENT_REWORK_RETRY` is absent" in refreshed
        assert "### Specialist workspace creation" in refreshed
        assert 'workspace_kind="worktree"' in refreshed
        assert "Do not fall back to `hermes kanban create`, `git worktree add`" in refreshed
        assert "### Dependency waiting state" in refreshed
        assert "Do not use `initial_status=blocked` merely because a parent is still open" in refreshed
        assert "### GitHub-backed root join" in refreshed
        assert "direct parent of the intake root" in refreshed
        assert "fresh-read the root dependency graph" in refreshed
        assert "do not call root `kanban_complete`" in refreshed
        assert refreshed.count(deployer.MARKER_BEGIN) == 1
        assert refreshed.count(deployer.MARKER_END) == 1


def test_canonical_role_contract_closes_specialist_graph_onto_root() -> None:
    role_contract = (ROOT / "docs/KANBAN_ROLE_CONTRACTS.md").read_text(encoding="utf-8")
    completion_contract = (ROOT / "docs/GITHUB_COMPLETION_LIFECYCLE.md").read_text(encoding="utf-8")

    assert "### GitHub-backed root join invariant" in role_contract
    assert "terminal specialist -> intake root" in role_contract
    assert "direct parent of the intake root" in role_contract
    assert "must not call root `kanban_complete`" in role_contract
    assert "task_links.child_id = intake_task.id" in completion_contract
    assert "Every direct parent must be" in completion_contract


def test_bounded_search_contract_is_triggered_and_selected_only() -> None:
    main = (CONTRACT_ROOT / "kanban-main-investigator.md").read_text(encoding="utf-8")
    investigator = (CONTRACT_ROOT / "kanban-investigator-SOUL.md").read_text(encoding="utf-8")
    reviewer = (CONTRACT_ROOT / "kanban-reviewer-investigator.md").read_text(encoding="utf-8")
    role_contract = (ROOT / "docs/KANBAN_ROLE_CONTRACTS.md").read_text(encoding="utf-8")
    req = (ROOT / ".agent/pr-requests/REQ-155-bounded-multi-investigator-search.md").read_text(encoding="utf-8")

    assert "default remains one Investigator" in main
    assert "h4v3-investigation-search-v1" in main
    assert "exactly two sibling" in main
    assert "bounded expansion" in main
    assert "candidate C" in main
    assert "selected handoff only" in main
    for trigger in (
        "LOW_CONFIDENCE_OR_BLOCKING_UNKNOWN",
        "IMPLEMENTATION_OR_REWORK_ROUND_GE_2",
        "RUNTIME_TEST_CONTRADICTION",
        "TRUSTED_REVIEWER_MODEL_REFRESH",
        "COMPETING_BOUNDARIES_UNRESOLVED",
        "NEW_EQUIVALENCE_CLASS_BYPASS_AFTER_PASS",
        "ACCEPTANCE_PASS_RUNTIME_ORACLE_FALSE_NEGATIVE",
    ):
        assert trigger in main
        assert trigger in role_contract
    for field in (
        "observed_failure", "root_cause_model", "generalized_invariant",
        "equivalence_classes", "falsification_plan", "completion_oracle",
        "residual_unknowns", "required_RED_regression", "preserved_contracts",
        "independence_basis",
    ):
        assert field in investigator
        assert field in role_contract
    assert "rework_class=implementation_gap" in reviewer
    assert "rework_class=investigation_model_refresh" in reviewer
    assert "investigation_model_refresh=true" in reviewer
    assert "HUMAN_VALIDATION_REQUIRED" in req
    assert "post-run" in req and "telemetry" in req



def test_specialist_terminal_handoff_is_explicitly_exempt_from_lifecycle_restrictions() -> None:
    investigator = (CONTRACT_ROOT / "kanban-investigator-SOUL.md").read_text(encoding="utf-8")
    developer = (CONTRACT_ROOT / "kanban-developer-investigator.md").read_text(encoding="utf-8")
    reviewer = (CONTRACT_ROOT / "kanban-reviewer-investigator.md").read_text(encoding="utf-8")
    role_contract = (ROOT / "docs/KANBAN_ROLE_CONTRACTS.md").read_text(encoding="utf-8")

    assert "mandatory terminal handoff of your own assigned Investigator task" in investigator
    assert "call `kanban_complete` on your own candidate task" in investigator
    assert "mandatory terminal handoff of the Developer's own assigned task" in developer
    assert "reviewer's own mandatory terminal handoff" in reviewer
    assert "not the worker's own mandatory terminal handoff" in role_contract
    assert "normally `kanban_complete` with the structured investigation handoff" in role_contract

def test_profile_deployer_fails_closed_before_any_write_when_investigator_missing() -> None:
    deployer = _load("investigator_profile_deployer_missing", DEPLOYER_PATH)
    with tempfile.TemporaryDirectory(prefix="investigator-profile-missing-") as directory:
        home = Path(directory) / ".hermes"
        originals = _profile_tree(home, include_investigator=False)

        with pytest.raises(deployer.DeployError, match="kanban-investigator"):
            deployer.deploy(home)

        for profile, original in originals.items():
            soul = home / "profiles" / profile / "SOUL.md"
            assert soul.read_text(encoding="utf-8") == original
            assert not (soul.parent / ".h4v3-backups").exists()
