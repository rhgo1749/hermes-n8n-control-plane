#!/usr/bin/env python3
"""Regression tests for the Hermes kanban_db_dispatch API cutover."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes-plugin/h4v3-resource-scheduler/__init__.py"
MOVED = {"has_spawnable_ready", "has_spawnable_review", "dispatch_once"}


def _load_plugin():
    spec = importlib.util.spec_from_file_location("h4v3_resource_scheduler_cutover_test", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _LegacyDbTrap(ModuleType):
    """A fake kanban_db whose removed compatibility aliases must never be read/written."""

    def __getattribute__(self, name: str):
        if name in MOVED:
            raise AssertionError(f"deprecated kanban_db alias read: {name}")
        return super().__getattribute__(name)

    def __setattr__(self, name: str, value):
        if name in MOVED:
            raise AssertionError(f"deprecated kanban_db alias write: {name}")
        super().__setattr__(name, value)


def test_moved_dispatch_symbols_route_only_to_canonical_module() -> None:
    plugin = _load_plugin()
    db = _LegacyDbTrap("fake_kanban_db")
    dispatch = ModuleType("fake_kanban_db_dispatch")

    ready = lambda conn: True
    review = lambda conn: False
    dispatch_once = lambda conn, **kwargs: "original"
    dispatch.has_spawnable_ready = ready
    dispatch.has_spawnable_review = review
    dispatch.dispatch_once = dispatch_once

    db.claim_task = lambda conn, task_id: (conn, task_id)
    db.kanban_db_path = lambda *, board: f"/{board}/kanban.db"

    facade = plugin._KanbanModuleFacade(db, dispatch)

    assert facade.has_spawnable_ready is ready
    assert facade.has_spawnable_review is review
    assert facade.dispatch_once is dispatch_once
    assert facade.claim_task is db.claim_task
    assert facade.kanban_db_path(board="alpha") == "/alpha/kanban.db"

    replacement_dispatch = lambda conn, **kwargs: "guarded"
    facade.dispatch_once = replacement_dispatch
    assert dispatch.dispatch_once is replacement_dispatch

    replacement_claim = lambda conn, task_id: "claim"
    facade.claim_task = replacement_claim
    assert db.claim_task is replacement_claim


def test_facade_routes_exactly_the_three_removed_compat_aliases() -> None:
    plugin = _load_plugin()
    assert plugin._DISPATCH_ATTRS == MOVED


def test_register_passes_split_facade_to_dynamic_installer(monkeypatch) -> None:
    plugin = _load_plugin()
    db = _LegacyDbTrap("hermes_cli.kanban_db")
    dispatch = ModuleType("hermes_cli.kanban_db_dispatch")
    dispatch.has_spawnable_ready = lambda conn: True
    dispatch.has_spawnable_review = lambda conn: True
    dispatch.dispatch_once = lambda conn, **kwargs: None
    db.claim_task = lambda *args, **kwargs: None
    db.claim_review_task = lambda *args, **kwargs: None

    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.kanban_db = db
    hermes_cli.kanban_db_dispatch = dispatch
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)

    admission = object()
    seen = {}

    class Dynamic:
        @staticmethod
        def install_core_claim_admission(facade, received_admission):
            seen["facade"] = facade
            seen["admission"] = received_admission
            # These are the exact accesses that emitted HermesPluginCompatWarning
            # before the hotfix. The trap proves none falls through to kanban_db.
            assert facade.has_spawnable_ready is dispatch.has_spawnable_ready
            assert facade.has_spawnable_review is dispatch.has_spawnable_review
            original = facade.dispatch_once
            facade.dispatch_once = original
            assert dispatch.dispatch_once is original
            assert facade.claim_task is db.claim_task

    def fake_load(_module_name: str, filename: str):
        if filename == "kanban_resource_admission.py":
            return admission
        if filename == "kanban_dynamic_resource.py":
            return Dynamic
        raise AssertionError(filename)

    monkeypatch.setattr(plugin, "_load_sibling_script", fake_load)
    plugin.register(object())

    assert seen["admission"] is admission
    assert isinstance(seen["facade"], plugin._KanbanModuleFacade)
