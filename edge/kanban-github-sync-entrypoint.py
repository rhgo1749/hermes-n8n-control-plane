#!/usr/bin/env python3
"""Deployment entrypoint for GitHub/Kanban edge reconciliation.

The large canonical reconciliation implementation stays in
``kanban-github-sync.py`` in the repository.  During deployment it is copied
beside this entrypoint as ``kanban-github-sync-core.py`` while this file is
installed under the historical live name ``kanban-github-sync.py``.

Keeping the admission overlay separate makes the scheduler extension small
and replaceable while preserving the existing reconciliation code and tests.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from kanban_resource_admission import install_resource_admission


def _core_path() -> Path:
    here = Path(__file__).resolve()
    deployed = here.with_name("kanban-github-sync-core.py")
    if deployed.is_file():
        return deployed

    # Repository checkout mode: the entrypoint sits beside the canonical
    # implementation under its normal source name.
    source = here.with_name("kanban-github-sync.py")
    if source != here and source.is_file():
        return source
    raise RuntimeError("canonical kanban-github-sync implementation is missing")


def _load_core() -> ModuleType:
    path = _core_path()
    name = "kanban_github_sync_core"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load edge sync core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    install_resource_admission(module)
    return module


_core = _load_core()

# Preserve import compatibility for callers/tests that load the historical
# live script and access its public or private helpers directly.
def __getattr__(name: str):
    return getattr(_core, name)


def _main(argv=None) -> int:
    return int(_core._main(argv))


if __name__ == "__main__":
    raise SystemExit(_main())
