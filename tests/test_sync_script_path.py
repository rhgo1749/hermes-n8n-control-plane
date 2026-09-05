"""Regression: ``_sync_script_path`` resolves the relocated edge sync script.

The edge sync script moved from a sibling of the intake core to
``edge/kanban-github-sync.py`` during a deployment-layout refactor. Running
the entrypoint straight from a repository checkout (rather than from the
deployed ``~/.hermes/scripts`` layout) used to fail closed with
``edge sync script is missing`` because only the sibling path was considered.

These tests pin the resolution contract:
  * an ``HERMES_KANBAN_SYNC_SCRIPT`` override wins when it is a file,
  * a missing override file fails closed,
  * with no override, the deployed sibling is preferred,
  * with no override and no sibling, the repository ``edge/`` layout is used.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "automation"
    / "hermes"
    / "scripts"
    / "github-agent-ready-kanban-intake.py"
)

spec = importlib.util.spec_from_file_location(
    "github_agent_ready_kanban_intake", MODULE_PATH
)
assert spec and spec.loader
intake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)


def _clear_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_SYNC_SCRIPT", raising=False)


def test_override_file_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "custom-sync.py"
    target.write_text("# custom\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_SYNC_SCRIPT", str(target))
    assert intake._sync_script_path() == target


def test_override_missing_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "does-not-exist.py"
    monkeypatch.setenv("HERMES_KANBAN_SYNC_SCRIPT", str(missing))
    with pytest.raises(intake.IntakeError) as exc:
        intake._sync_script_path()
    assert "edge sync script is missing" in str(exc.value)


def test_repo_checkout_layout_resolves_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No override, and no sibling next to the intake core in a repo checkout.
    _clear_override(monkeypatch)
    sibling = MODULE_PATH.parent / "kanban-github-sync.py"
    assert not sibling.is_file(), "repo checkout must not ship the sibling"
    resolved = intake._sync_script_path()
    expected = ROOT / "edge" / "kanban-github-sync.py"
    assert resolved == expected
    assert resolved.is_file()


def test_sibling_preferred_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # No override; in this repo checkout the deployed sibling is absent, so
    # resolution must fall through to the canonical edge/ layout.
    _clear_override(monkeypatch)
    sibling = MODULE_PATH.parent / "kanban-github-sync.py"
    assert not sibling.is_file(), "repo checkout must not ship the sibling"
    edge_candidate = MODULE_PATH.parents[3] / "edge" / "kanban-github-sync.py"
    assert edge_candidate.is_file()
    resolved = intake._sync_script_path()
    assert resolved == edge_candidate
    assert resolved == ROOT / "edge" / "kanban-github-sync.py"
