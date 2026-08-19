#!/usr/bin/env python3
"""Deployment entrypoint for GitHub Issue -> Kanban intake.

The canonical intake implementation remains
``github-agent-ready-kanban-intake.py`` in the repository. During deployment
it is installed beside this wrapper as
``github-agent-ready-kanban-intake-core.py`` while this file is installed under
the historical live name ``github-agent-ready-kanban-intake.py``.

This small overlay keeps Hermes core lifecycle semantics intact. GitHub-backed
workers finish their implementation run with core ``kanban_complete``; the
edge reconciler remains the only owner of GitHub ``done`` <-> ``review``
projection. This avoids spawning a second worker from a core ``review`` handoff
while the linked PR is merely waiting for a human merge.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


_OLD_COMPLETION_CONTRACT = """## GitHub completion contract (authoritative)

- Worker implementation completion is a review handoff: the Kanban status must be `review`, never `done`.
- `done` is allowed only after a fresh GitHub API read proves every PR linked to this Issue is merged into the target branch.
- An OPEN PR, CI success, pushed commit, PR creation, review handoff, or `Closes #N` text is not merge evidence.
- A CLOSED PR with `merged=false` is not completion evidence; keep the card in `review` (or preserve an existing human `blocked` state).
- GitHub API failure is fail-closed: preserve the current Kanban status and do not infer completion from local metadata or worker output.
- Linked PR discovery uses GitHub Issue links plus handoff references; all discovered required PRs must be merged."""

_NEW_COMPLETION_CONTRACT = """## GitHub completion contract (authoritative)

- Worker implementation completion must finish the worker run with core `kanban_complete`. That core `done` transition is provisional for a GitHub-backed card and is not merge evidence.
- Do not call `kanban_request_review` on this GitHub-backed intake card. Review waiting is projected by the edge reconciler, not by spawning a second core review worker.
- If any required PR is OPEN or CLOSED with `merged=false`, the edge reconciler projects the card to parked `review`, clears worker ownership/claim metadata, and leaves it non-runnable. Only an explicit trusted rework signal may return it to work.
- Authoritative `done` requires a fresh GitHub API read proving every PR linked to this Issue is merged into the target branch.
- An OPEN PR, CI success, pushed commit, PR creation, worker completion, or `Closes #N` text is not merge evidence.
- GitHub API failure is fail-closed: preserve the current Kanban status and do not infer completion from local metadata or worker output.
- Linked PR discovery uses GitHub Issue links plus handoff references; all discovered required PRs must be merged."""


def _core_path() -> Path:
    here = Path(__file__).resolve()
    deployed = here.with_name("github-agent-ready-kanban-intake-core.py")
    if deployed.is_file():
        return deployed

    # Repository checkout mode: the wrapper sits beside the canonical source.
    source = here.with_name("github-agent-ready-kanban-intake.py")
    if source != here and source.is_file():
        return source
    raise RuntimeError("canonical GitHub intake implementation is missing")


def _install_completion_contract_overlay(module: ModuleType) -> None:
    original = getattr(module, "_task_body", None)
    if not callable(original):
        raise RuntimeError("intake core has no _task_body")
    if getattr(original, "_github_completion_overlay_installed", False):
        return

    def patched_task_body(*args: Any, **kwargs: Any) -> str:
        rendered = str(original(*args, **kwargs))
        if _OLD_COMPLETION_CONTRACT not in rendered:
            raise RuntimeError(
                "GitHub completion contract drifted; refusing to emit an "
                "unverified worker lifecycle contract"
            )
        return rendered.replace(
            _OLD_COMPLETION_CONTRACT,
            _NEW_COMPLETION_CONTRACT,
            1,
        )

    patched_task_body._github_completion_overlay_installed = True  # type: ignore[attr-defined]
    patched_task_body._github_completion_overlay_original = original  # type: ignore[attr-defined]
    module._task_body = patched_task_body


def _load_core() -> ModuleType:
    path = _core_path()
    name = "github_agent_ready_kanban_intake_core"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load intake core: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _install_completion_contract_overlay(module)
    return module


_core = _load_core()


def __getattr__(name: str):
    return getattr(_core, name)


def main() -> int:
    return int(_core.main())


if __name__ == "__main__":
    raise SystemExit(main())
