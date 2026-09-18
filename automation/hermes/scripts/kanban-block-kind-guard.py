#!/usr/bin/env python3
"""Dispatch the approved H4V3 Kanban pre-tool guard command.

The historical block-kind hook command is already trusted by the live Hermes
shell-hook allowlist. Keep that exact command path stable and route lifecycle
policies behind it:

* ``kanban_block`` -> the preserved block-kind core guard;
* ``kanban_create`` -> structured retry/runtime canonicalization, specialist completion,
  durable materialization, and workspace-binding policies;
* ``terminal`` -> block-kind core first, then specialist/workspace policies.

Keeping one approved ``pre_tool_call`` command avoids introducing a second
shell-hook consent boundary during a hotfix. Structured execution canonicalization
fills the model-facing retry/runtime gap at the control-plane boundary; it does not
change Hermes core.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).resolve().parent
BLOCK_KIND_CORE = HERE / "kanban-block-kind-guard-core.py"
SPECIALIST_COMPLETION_GUARD = HERE / "kanban-specialist-completion-guard.py"
WORKSPACE_BINDING_GUARD = HERE / "kanban-workspace-binding-guard.py"
TASK_RETRY_LIMIT = 5
STANDARD_RUNTIME_SECONDS = 7200
LARGE_RUNTIME_SECONDS = 10800
SPECIALIST_ASSIGNEES = frozenset(
    {"kanban-investigator", "kanban-developer", "kanban-reviewer", "kanban-designer"}
)
SEARCH_SCHEMA = "h4v3-investigation-search-v1"


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
        raise RuntimeError(f"guard dependency missing: {SPECIALIST_COMPLETION_GUARD.name}")
    spec = importlib.util.spec_from_file_location(
        "h4v3_specialist_completion_guard", SPECIALIST_COMPLETION_GUARD
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load guard dependency: {SPECIALIST_COMPLETION_GUARD.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not callable(getattr(module, "evaluate_payload", None)):
        raise RuntimeError(
            f"guard dependency has no evaluate_payload: {SPECIALIST_COMPLETION_GUARD.name}"
        )
    return module


def _load_workspace_policy() -> ModuleType:
    if not WORKSPACE_BINDING_GUARD.is_file():
        raise RuntimeError(f"guard dependency missing: {WORKSPACE_BINDING_GUARD.name}")
    spec = importlib.util.spec_from_file_location(
        "h4v3_workspace_binding_for_retry", WORKSPACE_BINDING_GUARD
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load guard dependency: {WORKSPACE_BINDING_GUARD.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _is_blocked_quarantine(raw: Mapping[str, Any]) -> bool:
    value = raw.get("initial_status")
    return isinstance(value, str) and value.strip().casefold() == "blocked"


def _verify_retry_readback(workspace: ModuleType, raw: Mapping[str, Any]) -> None:
    board = raw.get("board") if isinstance(raw.get("board"), str) else None
    key = workspace._exact_idempotency_key(raw.get("idempotency_key"))
    db_path = Path(workspace._board_db_path(board)).expanduser()
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tasks)")}
        required = {
            "id",
            "assignee",
            "idempotency_key",
            "max_retries",
            "max_runtime_seconds",
            "created_at",
            "status",
        }
        if not required.issubset(columns):
            raise RuntimeError("board DB tasks schema cannot prove durable retry/runtime policy")
        rows = conn.execute(
            "SELECT id, assignee, max_retries, max_runtime_seconds, created_at FROM tasks "
            "WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC",
            (key,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise RuntimeError("materialized task disappeared before retry read-back")
    newest = rows[0]["created_at"]
    if sum(row["created_at"] == newest for row in rows) > 1:
        raise RuntimeError(
            "same-key rows have tied creation timestamps; retry read-back is ambiguous"
        )
    row = rows[0]
    if _normalize_assignee(row["assignee"]) != _normalize_assignee(raw.get("assignee")):
        raise RuntimeError("durable assignee does not match retry-compatible create")
    if row["max_retries"] != TASK_RETRY_LIMIT:
        raise RuntimeError("durable max_retries is not exactly five")
    if row["max_runtime_seconds"] != raw.get("max_runtime_seconds"):
        raise RuntimeError("durable max_runtime_seconds does not match canonical H4V3 runtime")


def _evaluate_retry_materializer(payload: Mapping[str, Any]) -> int:
    if str(payload.get("tool_name") or "") != "kanban_create":
        return 0
    raw = payload.get("tool_input")
    if not isinstance(raw, Mapping) or not _retry_compat_target(raw):
        return 0
    if raw.get("max_retries") != TASK_RETRY_LIMIT:
        raise RuntimeError("canonicalized structured create must carry max_retries=5")
    if raw.get("max_runtime_seconds") not in {STANDARD_RUNTIME_SECONDS, LARGE_RUNTIME_SECONDS}:
        raise RuntimeError("canonicalized structured create must carry a canonical H4V3 runtime")
    if _is_blocked_quarantine(raw):
        return 0
    workspace = _load_workspace_policy()
    binding = workspace._resolve_binding(raw)
    workspace._materialize_and_verify(raw, binding)
    _verify_retry_readback(workspace, raw)
    return 0


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
    """Resolve the interpreter that owns the active Hermes installation."""
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
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return env


def _run_python_policy(path: Path, payload: dict[str, Any]) -> int:
    if not path.is_file():
        return _hard_block(f"guard dependency missing: {path.name}")
    try:
        result = subprocess.run(
            [str(_hermes_python()), str(path)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env=_workspace_binding_subprocess_env(payload),
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return _hard_block(f"{path.name}: {type(exc).__name__}: {exc}")
    stdout = result.stdout or ""
    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        if stdout.strip():
            return _hard_block(f"{path.name} emitted unexpected output on allow")
        return 0
    if result.returncode == 2 and stdout:
        sys.stdout.write(stdout)
        return 2
    return _hard_block(stderr or f"{path.name} exited {result.returncode}")


def _run_workspace_binding_policy(payload: dict[str, Any]) -> int:
    return _run_python_policy(WORKSPACE_BINDING_GUARD, payload)


def _run_retry_materializer(payload: dict[str, Any]) -> int:
    try:
        result = subprocess.run(
            [str(_hermes_python()), str(Path(__file__).resolve()), "--materialize-retry"],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env=_workspace_binding_subprocess_env(payload),
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return _hard_block(
            f"structured retry materializer: {type(exc).__name__}: {exc}"
        )
    stdout = result.stdout or ""
    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        if stdout.strip():
            return _hard_block(
                "structured retry materializer emitted unexpected output on allow"
            )
        return 0
    if result.returncode == 2 and stdout:
        sys.stdout.write(stdout)
        return 2
    return _hard_block(stderr or f"structured retry materializer exited {result.returncode}")


def _normalize_assignee(value: Any) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _is_search_selector(raw: Mapping[str, Any]) -> bool:
    if _normalize_assignee(raw.get("assignee")) != "kanban-main":
        return False
    marker = raw.get("investigation_search")
    return (
        isinstance(marker, Mapping)
        and marker.get("schema_id") == SEARCH_SCHEMA
        and marker.get("phase") == "selector"
    )


def _retry_compat_target(raw: Mapping[str, Any]) -> bool:
    return (
        _normalize_assignee(raw.get("assignee")) in SPECIALIST_ASSIGNEES
        or _is_search_selector(raw)
    )


def _body_marks_large_runtime(raw: Mapping[str, Any]) -> bool:
    body = raw.get("body")
    if not isinstance(body, str):
        return False
    for line in body.splitlines():
        marker = line.strip()
        if marker.startswith("- "):
            marker = marker[2:].strip()
        if marker.strip("`") == "runtime_class=large":
            return True
    return False


def _is_investigation_search(raw: Mapping[str, Any]) -> bool:
    marker = raw.get("investigation_search")
    return isinstance(marker, Mapping) and marker.get("schema_id") == SEARCH_SCHEMA


def _canonical_runtime_seconds(raw: Mapping[str, Any]) -> int:
    if _is_investigation_search(raw):
        return STANDARD_RUNTIME_SECONDS
    assignee = _normalize_assignee(raw.get("assignee"))
    if (
        assignee in {"kanban-developer", "kanban-reviewer", "kanban-designer"}
        and _body_marks_large_runtime(raw)
    ):
        return LARGE_RUNTIME_SECONDS
    return STANDARD_RUNTIME_SECONDS


def _canonicalize_search_runtime_marker(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    marker = raw.get("investigation_search")
    if not isinstance(marker, Mapping):
        return None
    normalized_marker = dict(marker)
    budget = marker.get("budget")
    if isinstance(budget, Mapping):
        normalized_budget = dict(budget)
        normalized_budget["max_runtime_seconds"] = STANDARD_RUNTIME_SECONDS
        normalized_marker["budget"] = normalized_budget
    return normalized_marker


def _canonicalize_structured_retry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize H4V3 execution retry/runtime policy before admission.

    This is a pure payload transformation. It performs no DB/task mutation, so
    bounded-search admission still runs before the materializer creates a row.
    The model does not choose the standard wall-time: ordinary H4V3 execution
    is always 7200 seconds, while an explicitly marked large implementation is
    10800 seconds. Search candidate/selector marker budgets are normalized to
    the same 7200-second bound. Explicit retry values other than five fail
    closed rather than being silently overwritten.
    """
    if str(payload.get("tool_name") or "") != "kanban_create":
        return payload
    raw = payload.get("tool_input")
    if not isinstance(raw, Mapping) or not _retry_compat_target(raw):
        return payload
    current = raw.get("max_retries")
    if current is not None and (
        isinstance(current, bool) or current != TASK_RETRY_LIMIT
    ):
        raise ValueError("H4V3 execution task max_retries must be exactly five")
    normalized = dict(payload)
    normalized_input = dict(raw)
    normalized_input["max_retries"] = TASK_RETRY_LIMIT
    normalized_input["max_runtime_seconds"] = _canonical_runtime_seconds(raw)
    marker = _canonicalize_search_runtime_marker(raw)
    if marker is not None:
        normalized_input["investigation_search"] = marker
    normalized["tool_input"] = normalized_input
    return normalized


def _run_specialist_policies(payload: dict[str, Any]) -> int:
    prepared = payload
    if str(payload.get("tool_name") or "") == "kanban_create":
        try:
            prepared = _canonicalize_structured_retry_payload(payload)
        except ValueError as exc:
            return _hard_block(str(exc))

    decision = _run_specialist_policy(prepared)
    if decision != 0:
        return decision

    if str(prepared.get("tool_name") or "") == "kanban_create":
        decision = _run_retry_materializer(prepared)
        if decision != 0:
            return decision

    return _run_workspace_binding_policy(prepared)


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

    if sys.argv[1:] == ["--materialize-retry"]:
        try:
            return _evaluate_retry_materializer(payload)
        except Exception as exc:
            return _hard_block(
                f"structured retry materializer: {type(exc).__name__}: {exc}"
            )

    tool_name = str(payload.get("tool_name") or "")
    if tool_name == "kanban_block":
        return _delegate_block_kind(raw)
    if tool_name == "kanban_create":
        return _run_specialist_policies(payload)
    if tool_name != "terminal":
        return 0

    returncode, stdout, stderr = _run_block_kind(raw)
    if returncode != 0:
        if returncode == 2 and stdout:
            sys.stdout.write(stdout)
            return 2
        return _hard_block(stderr.strip() or f"{BLOCK_KIND_CORE.name} exited {returncode}")
    if stdout:
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
