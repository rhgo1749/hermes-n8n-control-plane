"""H4V3 backend-aware Kanban resource scheduler.

Installed into the dispatcher-owning Hermes profile. Registration patches the
core Kanban claim boundary so scarce local inference resources are admitted
cross-board while cloud-backed profiles remain parallel-capable.

Hermes split dispatcher helpers out of ``hermes_cli.kanban_db`` into
``hermes_cli.kanban_db_dispatch``.  The old facade aliases are temporary
compatibility shims and are scheduled for removal.  Keep DB/claim mutations on
``kanban_db`` while routing dispatcher health probes and ``dispatch_once`` to
the canonical dispatch module so the plugin survives that cutover without
compat warnings.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


_DISPATCH_ATTRS = frozenset(
    {"has_spawnable_ready", "has_spawnable_review", "dispatch_once"}
)


class _KanbanModuleFacade:
    """Route moved dispatcher symbols to their canonical Hermes module.

    ``kanban_dynamic_resource.install_core_claim_admission`` predates Hermes'
    module split and accepts one module-like object for both DB helpers and
    dispatcher hooks.  This narrow facade preserves that API while ensuring
    reads/writes of moved symbols never touch the deprecated ``kanban_db``
    compatibility aliases.
    """

    def __init__(self, kanban_db: ModuleType, kanban_db_dispatch: ModuleType) -> None:
        object.__setattr__(self, "_kanban_db", kanban_db)
        object.__setattr__(self, "_kanban_db_dispatch", kanban_db_dispatch)

    def _target(self, name: str) -> ModuleType:
        if name in _DISPATCH_ATTRS:
            return object.__getattribute__(self, "_kanban_db_dispatch")
        return object.__getattribute__(self, "_kanban_db")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(name), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._target(name), name, value)


def _load_sibling_script(module_name: str, filename: str) -> ModuleType:
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    path = hermes_home / "scripts" / filename
    if not path.is_file():
        raise RuntimeError(f"H4V3 resource scheduler dependency missing: {path}")
    existing = sys.modules.get(module_name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == path.resolve():
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load H4V3 resource scheduler dependency: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def register(ctx) -> None:  # type: ignore[no-untyped-def]
    del ctx
    admission = _load_sibling_script(
        "h4v3_kanban_resource_admission",
        "kanban_resource_admission.py",
    )
    dynamic = _load_sibling_script(
        "h4v3_kanban_dynamic_resource",
        "kanban_dynamic_resource.py",
    )
    from hermes_cli import kanban_db, kanban_db_dispatch

    dynamic.install_core_claim_admission(
        _KanbanModuleFacade(kanban_db, kanban_db_dispatch),
        admission,
    )
