#!/usr/bin/env python3
"""Dispatch the approved H4V3 Kanban pre-tool guard command.

The historical block-kind hook command is already trusted by the live Hermes
shell-hook allowlist. Keep that exact command path stable and route both
lifecycle policies behind it:

* ``kanban_block`` -> the preserved block-kind core guard;
* ``kanban_create`` -> the specialist completion-contract guard;
* ``terminal`` -> block-kind core first, then specialist completion guard.

Keeping one approved ``pre_tool_call`` command avoids introducing a second
shell-hook consent boundary during a hotfix. A child guard failure is converted
to a fail-closed exit-2 block instead of relying on the outer shell-hook parser
to interpret an arbitrary nonzero exit.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BLOCK_KIND_CORE = HERE / "kanban-block-kind-guard-core.py"
SPECIALIST_COMPLETION_GUARD = HERE / "kanban-specialist-completion-guard.py"


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


def _run_guard(path: Path, raw: str) -> tuple[int, str, str]:
    if not path.is_file():
        return 127, "", f"guard dependency missing: {path.name}"
    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            input=raw,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 126, "", f"{path.name}: {type(exc).__name__}: {exc}"
    return result.returncode, result.stdout or "", result.stderr or ""


def _delegate(path: Path, raw: str) -> int:
    returncode, stdout, stderr = _run_guard(path, raw)
    if returncode == 0:
        # A successful guard should normally emit nothing. Preserve any valid
        # response it deliberately returned so the outer hook can parse it.
        if stdout:
            sys.stdout.write(stdout)
        return 0
    if returncode == 2:
        if stdout:
            sys.stdout.write(stdout)
            return 2
        return _hard_block(stderr.strip() or f"{path.name} blocked without a diagnostic")
    return _hard_block(stderr.strip() or f"{path.name} exited {returncode}")


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return _hard_block(f"malformed pre_tool_call payload: {type(exc).__name__}: {exc}")
    if not isinstance(payload, dict):
        return 0

    tool_name = str(payload.get("tool_name") or "")
    if tool_name == "kanban_block":
        return _delegate(BLOCK_KIND_CORE, raw)
    if tool_name == "kanban_create":
        return _delegate(SPECIALIST_COMPLETION_GUARD, raw)
    if tool_name != "terminal":
        return 0

    # terminal is shared by both policies. The first guard returns zero/no
    # output for unrelated commands; only then is the second policy evaluated.
    returncode, stdout, stderr = _run_guard(BLOCK_KIND_CORE, raw)
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
    return _delegate(SPECIALIST_COMPLETION_GUARD, raw)


if __name__ == "__main__":
    raise SystemExit(main())
