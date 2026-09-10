#!/usr/bin/env python3
"""Regression coverage for the dynamic-resource dispatcher API wrapper."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
DYNAMIC = ROOT / "edge/kanban_dynamic_resource.py"
DYNAMIC_CORE = ROOT / "edge/kanban_dynamic_resource_core.py"
RESOURCE_INSTALLER = ROOT / "automation/hermes/scripts/install-h4v3-resource-scheduler.sh"
EDGE_DEPLOYER = ROOT / "automation/hermes/scripts/deploy-intake-edge.sh"
MOVED = {"has_spawnable_ready", "has_spawnable_review", "dispatch_once"}


def _load_dynamic():
    spec = importlib.util.spec_from_file_location(
        "h4v3_dynamic_resource_wrapper_cutover_test",
        DYNAMIC,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _LegacyDbTrap(ModuleType):
    """Fail if the removed dispatcher aliases are touched on kanban_db."""

    def __getattribute__(self, name: str):
        if name in MOVED:
            raise AssertionError(f"deprecated kanban_db alias read: {name}")
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value):
        if name in MOVED:
            raise AssertionError(f"deprecated kanban_db alias write: {name}")
        super().__setattr__(name, value)


def test_direct_real_db_caller_is_split_before_core_warning_sites(monkeypatch) -> None:
    dynamic = _load_dynamic()
    db = _LegacyDbTrap("hermes_cli.kanban_db")
    dispatch = ModuleType("hermes_cli.kanban_db_dispatch")

    dispatch.has_spawnable_ready = lambda conn: True
    dispatch.has_spawnable_review = lambda conn: False
    dispatch.dispatch_once = lambda conn, **kwargs: None
    db.claim_task = lambda *args, **kwargs: None
    db.claim_review_task = lambda *args, **kwargs: None

    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.kanban_db_dispatch = dispatch
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)

    split = dynamic._split_dispatch_surface(db)
    assert split is not db
    assert split.claim_task is db.claim_task
    assert split.has_spawnable_ready is dispatch.has_spawnable_ready
    assert split.has_spawnable_review is dispatch.has_spawnable_review
    assert split.dispatch_once is dispatch.dispatch_once

    # Exercise the exact core installation sites that emitted
    # HermesPluginCompatWarning in the live CLI. The legacy trap would fail
    # immediately if either helper fell back to kanban_db.*.
    admission = object()
    dynamic._core._install_resource_health_probes(split, admission)
    dynamic._core._install_dispatch_overlay(split, admission)

    assert getattr(dispatch.has_spawnable_ready, "_h4v3_resource_health_installed", False)
    assert getattr(dispatch.has_spawnable_review, "_h4v3_resource_health_installed", False)
    assert getattr(dispatch.dispatch_once, "_h4v3_resource_dispatch_installed", False)


def test_non_hermes_test_double_keeps_legacy_single_surface() -> None:
    dynamic = _load_dynamic()
    fake = ModuleType("fixture_kanban_db")
    fake.has_spawnable_ready = lambda conn: True
    assert dynamic._split_dispatch_surface(fake) is fake


def test_wrapper_keeps_original_core_as_sibling() -> None:
    assert DYNAMIC_CORE.is_file()
    assert DYNAMIC_CORE.stat().st_size > 50_000
    text = DYNAMIC.read_text(encoding="utf-8")
    assert "kanban_dynamic_resource_core.py" in text
    assert "_split_dispatch_surface" in text
    assert MOVED == {"has_spawnable_ready", "has_spawnable_review", "dispatch_once"}


def test_resource_installer_deploys_core_before_wrapper() -> None:
    text = RESOURCE_INSTALLER.read_text(encoding="utf-8")
    core = 'install_script "$DYNAMIC_CORE_SOURCE" "$TARGET_DYNAMIC_CORE"'
    wrapper = 'install_script "$DYNAMIC_SOURCE" "$TARGET_DYNAMIC"'
    assert core in text and wrapper in text
    assert text.index(core) < text.index(wrapper)


def test_edge_deployer_deploys_core_before_wrapper() -> None:
    text = EDGE_DEPLOYER.read_text(encoding="utf-8")
    core = 'cp -p "$EDGE_DYNAMIC_CORE_SOURCE" "$CANDIDATE/kanban_dynamic_resource_core.py"'
    wrapper = 'cp -p "$EDGE_DYNAMIC_SOURCE" "$CANDIDATE/kanban_dynamic_resource.py"'
    assert core in text and wrapper in text
    assert text.index(core) < text.index(wrapper)
    assert "kanban_dynamic_resource_core.py" in text.split("# 3) backups", 1)[1]
