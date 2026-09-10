#!/usr/bin/env python3
"""Stable entrypoint for H4V3 dynamic Kanban resource admission.

The implementation is kept in ``kanban_dynamic_resource_core.py``.  This thin
entrypoint owns Hermes API compatibility at the module boundary: September
2026 split dispatcher helpers out of ``hermes_cli.kanban_db`` into
``hermes_cli.kanban_db_dispatch``, while claim/database helpers remain on the
original module.

Any caller that still supplies the real ``hermes_cli.kanban_db`` module (or a
module-like facade over it) is transparently split before the core installer
runs.  This prevents reads/writes of the temporary compatibility aliases and
keeps the resource scheduler functional after those aliases are removed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


_CORE_MODULE_NAME = "h4v3_kanban_dynamic_resource_core"
_CORE_PATH = Path(__file__).with_name("kanban_dynamic_resource_core.py")
_MOVED_DISPATCH_ATTRS = frozenset(
    {"has_spawnable_ready", "has_spawnable_review", "dispatch_once"}
)


def _load_core() -> ModuleType:
    existing = sys.modules.get(_CORE_MODULE_NAME)
    if existing is not None:
        existing_path = Path(getattr(existing, "__file__", ""))
        try:
            if existing_path.resolve() == _CORE_PATH.resolve():
                return existing
        except OSError:
            pass

    if not _CORE_PATH.is_file():
        raise RuntimeError(f"dynamic resource core missing: {_CORE_PATH}")
    spec = importlib.util.spec_from_file_location(_CORE_MODULE_NAME, _CORE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load dynamic resource core: {_CORE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_CORE_MODULE_NAME, None)
        raise
    return module


_core = _load_core()

# Preserve the historical module surface, including private helpers used by the
# focused repository tests.  Functions keep their original core globals; only
# the two installer entrypoints below are compatibility-owned here.
for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)


class _KanbanDispatchFacade:
    """Route only moved dispatcher symbols to the canonical dispatch module."""

    def __init__(self, kanban_db: Any, kanban_db_dispatch: ModuleType) -> None:
        object.__setattr__(self, "_kanban_db", kanban_db)
        object.__setattr__(self, "_kanban_db_dispatch", kanban_db_dispatch)

    def _target(self, name: str) -> Any:
        if name in _MOVED_DISPATCH_ATTRS:
            return object.__getattribute__(self, "_kanban_db_dispatch")
        return object.__getattribute__(self, "_kanban_db")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target(name), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(self._target(name), name, value)


def _split_dispatch_surface(kanban_db: Any) -> Any:
    """Return a split surface only for the real Hermes legacy DB module.

    Test doubles and pre-split Hermes builds retain their existing behavior.
    A facade over ``hermes_cli.kanban_db`` also reports that module name through
    ``__getattr__`` and is therefore safely normalized here.
    """
    try:
        module_name = str(getattr(kanban_db, "__name__", "") or "")
    except Exception:
        return kanban_db
    if module_name != "hermes_cli.kanban_db":
        return kanban_db

    try:
        from hermes_cli import kanban_db_dispatch
    except Exception:
        # Backward compatibility with Hermes versions from before the split.
        return kanban_db
    return _KanbanDispatchFacade(kanban_db, kanban_db_dispatch)


def install_core_claim_admission(kanban_db: Any, admission_module: Any) -> None:
    """Install core admission without touching deprecated dispatcher aliases."""
    _core.install_core_claim_admission(
        _split_dispatch_surface(kanban_db),
        admission_module,
    )


def install_everywhere(admission_module: Any, kanban_db: Any | None = None) -> None:
    """Preserve the historical installer API with the same split normalization."""
    if kanban_db is None:
        _core.install_everywhere(admission_module)
        return
    _core.install_everywhere(
        admission_module,
        _split_dispatch_surface(kanban_db),
    )
