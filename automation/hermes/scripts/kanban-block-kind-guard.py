#!/usr/bin/env python3
"""Dispatch the approved H4V3 Kanban pre-tool guard command.

The historical block-kind hook command is already trusted by the live Hermes
shell-hook allowlist. Keep that exact command path stable and route both
lifecycle policies behind it:

* ``kanban_block`` -> the preserved block-kind core guard;
* ``kanban_create`` -> specialist completion and workspace-binding policies;
* ``terminal`` -> block-kind core first, then both specialist policies.

Keeping one approved ``pre_tool_call`` command avoids introducing a second
shell-hook consent boundary during a hotfix. The completion policy stays
in-process. The workspace-binding policy runs under the Python interpreter
beside the active ``hermes`` launcher so its canonical ``hermes_cli`` DB/parser
imports do not depend on the shell-hook subprocess cwd or the generic
``python3`` selected by PATH.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
BLOCK_KIND_CORE = HERE / "kanban-block-kind-guard-core.py"
SPECIALIST_COMPLETION_GUARD = HERE / "kanban-specialist-completion-guard.py"
WORKSPACE_BINDING_GUARD = HERE / "kanban-workspace-binding-guard.py"


def _hard_block(message: str) -> int:
    print(
        json.dumps(
            {
                "action": "block",
                "message": f"H4V3 lifecycle guard failed closed: {message}. No task mutation was performed.",
            },
            ensure_ascii=False,
        )
    )
    return 2


def _load_specialist_policy() -> ModuleType:
    if not SPECIALIST_COMPLETION_GUARD.is_file():
        raise RuntimeError(
            f"guard dependency missing: {SPECIALIST_COMPLETION_GUARD.name}"
        )
    spec = importlib.util.spec_from_file_location(
        "h4v3_specialist_completion_guard",
        SPECIALIST_COMPLETION_GUARD,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            f"cannot load guard dependency: {SPECIALIST_COMPLETION_GUARD.name}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "evaluate_payload", None)):
        raise RuntimeError(
            f"guard dependency has no evaluate_payload: {SPECIALIST_COMPLETION_GUARD.name}"
        )
    return module


def _run_block_kind(raw: str) -> tuple[int, str, str]:
    if not BLOCK_KIND_CORE.is_file():
        return 127, "", f"guard dependency missing: {BLOCK_KIND_CORE.name}"
    try:
        result = subprocess.run(
            [sys.executable, str(BLOCK_KIND_CORE)],
            input=raw,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 126, "", f"{BLOCK_KIND_CORE.name}: {type(exc).__name__}: {exc}"
    return result.returncode, result.stdout or "", result.stderr or ""


def _delegate_block_kind(raw: str) -> int:
    returncode, stdout, stderr = _run_block_kind(raw)
    if returncode == 0:
        if stdout:
            sys.stdout.write(stdout)
        return 0
    if returncode == 2:
        if stdout:
            sys.stdout.write(stdout)
            return 2
        return _hard_block(
            stderr.strip() or f"{BLOCK_KIND_CORE.name} blocked without a diagnostic"
        )
    return _hard_block(stderr.strip() or f"{BLOCK_KIND_CORE.name} exited {returncode}")


def _run_specialist_policy(payload: dict[str, Any]) -> int:
    try:
        module = _load_specialist_policy()
        return int(module.evaluate_payload(payload))
    except Exception as exc:
        return _hard_block(
            f"{SPECIALIST_COMPLETION_GUARD.name}: {type(exc).__name__}: {exc}"
        )


def _hermes_python() -> Path:
    """Resolve the interpreter that owns the active Hermes installation.

    Shell hooks intentionally keep the historical command ``python3 <guard>``
    for consent identity. In the container runtime that generic ``python3`` is
    not the Hermes venv and cannot import ``hermes_cli``. The launcher itself
    is stable and resolves into its owning venv, so use its sibling interpreter
    for the workspace policy that must call the canonical Kanban DB owner.
    """
    launcher = shutil.which("hermes")
    if not launcher:
        raise RuntimeError("active hermes launcher is not on PATH")
    bin_dir = Path(launcher).resolve().parent
    for name in ("python3", "python"):
        candidate = bin_dir / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(f"Hermes Python interpreter is missing beside {bin_dir / 'hermes'}")


def _workspace_binding_subprocess_env(payload: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    if str(payload.get("tool_name") or "") == "kanban_create":
        # Shell hooks are ordinary worker descendants. Hermes therefore stamps the
        # hook subprocess with HERMES_DELEGATED_CHILD_CONTEXT even when the caller
        # is the dispatcher-owned root worker. The workspace guard intentionally
        # calls canonical create_task() to atomically materialize+verify a valid
        # structured create, so carrying that descendant fence into the nested
        # guard makes every legitimate root create fail before mutation.
        #
        # Remove the subprocess-only fence for the *native structured tool* path.
        # Hermes hides/refuses kanban_* tools for real delegate_task children before
        # pre_tool_call hooks run, so this does not grant a delegated child a board
        # mutation route. Keep the fence for terminal: a delegated child can invoke
        # terminal, and H4V3 explicitly forbids CLI/ad-hoc create as a fallback.
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return env


def _run_workspace_binding_policy(payload: dict[str, Any]) -> int:
    if not WORKSPACE_BINDING_GUARD.is_file():
        return _hard_block(f"guard dependency missing: {WORKSPACE_BINDING_GUARD.name}")
    try:
        result = subprocess.run(
            [str(_hermes_python()), str(WORKSPACE_BINDING_GUARD)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env=_workspace_binding_subprocess_env(payload),
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return _hard_block(
            f"{WORKSPACE_BINDING_GUARD.name}: {type(exc).__name__}: {exc}"
        )
    stdout = result.stdout or ""
    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        if stdout.strip():
            return _hard_block(
                f"{WORKSPACE_BINDING_GUARD.name} emitted unexpected output on allow"
            )
        return 0
    if result.returncode == 2 and stdout:
        sys.stdout.write(stdout)
        return 2
    return _hard_block(
        stderr or f"{WORKSPACE_BINDING_GUARD.name} exited {result.returncode}"
    )


def _run_specialist_policies(payload: dict[str, Any]) -> int:
    decision = _run_specialist_policy(payload)
    if decision != 0:
        return decision
    return _run_workspace_binding_policy(payload)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _hard_block(
            f"malformed pre_tool_call payload: {type(exc).__name__}: {exc}"
        )
    if not isinstance(payload, dict):
        return 0

    tool_name = str(payload.get("tool_name") or "")
    if tool_name == "kanban_block":
        return _delegate_block_kind(raw)
    if tool_name == "kanban_create":
        return _run_specialist_policies(payload)
    if tool_name != "terminal":
        return 0

    # terminal is shared by both policies. The historical block-kind policy
    # runs first; only if it allows the command do we evaluate specialist task
    # creation semantics.
    returncode, stdout, stderr = _run_block_kind(raw)
    if returncode != 0:
        if returncode == 2 and stdout:
            sys.stdout.write(stdout)
            return 2
        return _hard_block(stderr.strip() or f"{BLOCK_KIND_CORE.name} exited {returncode}")
    if stdout:
        # Defensive: a successful blocking guard must not silently emit an
        # unexpected directive and then have the wrapper ignore it.
        try:
            directive = json.loads(stdout)
        except json.JSONDecodeError:
            return _hard_block(f"{BLOCK_KIND_CORE.name} emitted unparseable output")
        if isinstance(directive, dict) and directive.get("action") == "block":
            sys.stdout.write(stdout)
            return 2
    return _run_specialist_policies(payload)


if __name__ == "__main__":
    raise SystemExit(main())
