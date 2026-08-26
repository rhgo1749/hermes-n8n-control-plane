#!/usr/bin/env python3
"""Deployment entrypoint for GitHub/Kanban edge reconciliation.

The large canonical reconciliation implementation stays in
``kanban-github-sync.py`` in the repository. During deployment it is copied
beside this entrypoint as ``kanban-github-sync-core.py`` while this file is
installed under the historical live name ``kanban-github-sync.py``.

Small, independently reviewable overlays are installed here so the canonical
state machine can stay unchanged: resource admission controls worker capacity,
dynamic backend resolution (when installed) makes that admission follow each
profile's current provider/endpoint, head-binding feedback adds observational
PR guidance without changing rework transitions, and the retry-signal guard
prevents edge-owned help text from being consumed as a fresh maintainer retry.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import kanban_resource_admission as resource_admission
from kanban_head_binding_feedback import install_head_binding_feedback
from kanban_retry_signal_guard import install_retry_signal_guard
try:
    from kanban_workspace_admission import install_workspace_admission
except ImportError:  # backwards-compatible deploy before the overlay exists
    install_workspace_admission = None  # type: ignore[assignment]

try:
    from kanban_dynamic_resource import install_everywhere
except ImportError:  # backwards-compatible deploy before scheduler install
    install_everywhere = None  # type: ignore[assignment]


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

    # Patch policy parsing and cross-board helpers before the legacy edge
    # admission wrapper captures them. Core READY/REVIEW claim admission is
    # installed separately by the h4v3-resource-scheduler user plugin in the
    # long-lived gateway process. If an older deployment has not installed
    # the dynamic module yet, preserve the previous assignee-only behavior.
    if install_everywhere is not None:
        install_everywhere(resource_admission)
    resource_admission.install_resource_admission(module)
    install_head_binding_feedback(module)
    install_retry_signal_guard(module)
    if install_workspace_admission is not None:
        install_workspace_admission(module)
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
