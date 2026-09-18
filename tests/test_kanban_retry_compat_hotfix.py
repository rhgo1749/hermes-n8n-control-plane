from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "automation/hermes/scripts/kanban-block-kind-guard.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _selector_input() -> dict[str, Any]:
    return {
        "title": "bounded selector",
        "assignee": "kanban-main",
        "workspace_kind": "worktree",
        "project": "ctrl-hangul",
        "idempotency_key": "github:ctrl-hangul:113:round:2:selector",
        "max_runtime_seconds": 1800,
        "investigation_search": {
            "schema_id": "h4v3-investigation-search-v1",
            "phase": "selector",
            "budget": {"max_runtime_seconds": 1800},
        },
    }


def test_structured_retry_compat_injects_five_for_specialist_and_selector() -> None:
    guard = _load("retry_wrapper_test", WRAPPER)
    for raw in (
        {
            "title": "candidate A",
            "assignee": "kanban-investigator",
            "workspace_kind": "worktree",
            "project": "ctrl-hangul",
            "idempotency_key": "candidate-a",
        },
        _selector_input(),
    ):
        original = {"tool_name": "kanban_create", "tool_input": raw}
        prepared = guard._canonicalize_structured_retry_payload(original)
        assert prepared is not original
        assert prepared["tool_input"]["max_retries"] == 5
        assert prepared["tool_input"]["max_runtime_seconds"] == 7200
        marker = prepared["tool_input"].get("investigation_search")
        if isinstance(marker, dict):
            assert marker["budget"]["max_runtime_seconds"] == 7200
        assert "max_retries" not in raw


def test_structured_runtime_canonicalizes_large_implementation() -> None:
    guard = _load("retry_wrapper_large_runtime", WRAPPER)
    payload = {
        "tool_name": "kanban_create",
        "tool_input": {
            "title": "large implementation",
            "body": "runtime_class=large",
            "assignee": "kanban-developer",
            "idempotency_key": "large-dev",
            "max_runtime_seconds": 1800,
        },
    }
    prepared = guard._canonicalize_structured_retry_payload(payload)
    assert prepared["tool_input"]["max_runtime_seconds"] == 10800
    assert payload["tool_input"]["max_runtime_seconds"] == 1800


def test_large_runtime_requires_explicit_marker_line() -> None:
    guard = _load("retry_wrapper_large_marker_line", WRAPPER)
    payload = {
        "tool_name": "kanban_create",
        "tool_input": {
            "title": "ordinary implementation",
            "body": "Do not treat the phrase runtime_class=large in prose as authorization.",
            "assignee": "kanban-developer",
            "idempotency_key": "ordinary-dev",
            "max_runtime_seconds": 10800,
        },
    }
    prepared = guard._canonicalize_structured_retry_payload(payload)
    assert prepared["tool_input"]["max_runtime_seconds"] == 7200


def test_structured_retry_compat_leaves_ordinary_main_untouched() -> None:
    guard = _load("retry_wrapper_ordinary_main", WRAPPER)
    payload = {
        "tool_name": "kanban_create",
        "tool_input": {
            "title": "ordinary main",
            "assignee": "kanban-main",
            "idempotency_key": "main-normal",
        },
    }
    assert guard._canonicalize_structured_retry_payload(payload) is payload


def test_structured_retry_compat_rejects_explicit_non_five() -> None:
    guard = _load("retry_wrapper_nonfive", WRAPPER)
    payload = {
        "tool_name": "kanban_create",
        "tool_input": {
            "title": "candidate A",
            "assignee": "kanban-investigator",
            "idempotency_key": "candidate-a",
            "max_retries": 4,
        },
    }
    with pytest.raises(ValueError, match="exactly five"):
        guard._canonicalize_structured_retry_payload(payload)


def test_materializer_persists_and_reads_back_selector_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materializer = _load("retry_materializer_test", WRAPPER)
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, assignee TEXT, idempotency_key TEXT, "
        "max_retries INTEGER, max_runtime_seconds INTEGER, created_at INTEGER, status TEXT)"
    )
    conn.commit()
    conn.close()

    class FakeWorkspace:
        @staticmethod
        def _exact_idempotency_key(value: Any) -> str:
            assert isinstance(value, str) and value
            return value

        @staticmethod
        def _board_db_path(board: Any) -> Path:
            return db

        @staticmethod
        def _resolve_binding(raw: Any) -> object:
            assert raw["project"] == "ctrl-hangul"
            return object()

        @staticmethod
        def _materialize_and_verify(raw: Any, binding: Any) -> None:
            del binding
            with sqlite3.connect(db) as local:
                local.execute(
                    "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "t_selector",
                        raw["assignee"],
                        raw["idempotency_key"],
                        raw["max_retries"],
                        raw["max_runtime_seconds"],
                        1,
                        "todo",
                    ),
                )
                local.commit()

    monkeypatch.setattr(materializer, "_load_workspace_policy", lambda: FakeWorkspace)
    prepared = materializer._canonicalize_structured_retry_payload(
        {"tool_name": "kanban_create", "tool_input": _selector_input()}
    )
    raw = prepared["tool_input"]
    rc = materializer._evaluate_retry_materializer(prepared)
    assert rc == 0
    with sqlite3.connect(db) as check:
        assert check.execute(
            "SELECT assignee, max_retries, max_runtime_seconds FROM tasks "
            "WHERE id = 't_selector'"
        ).fetchone() == ("kanban-main", 5, 7200)


def test_completion_admission_runs_before_retry_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = _load("retry_wrapper_order", WRAPPER)
    events: list[str] = []
    monkeypatch.setattr(
        guard, "_run_specialist_policy", lambda payload: events.append("admit") or 0
    )
    monkeypatch.setattr(
        guard, "_run_retry_materializer", lambda payload: events.append("materialize") or 0
    )
    monkeypatch.setattr(
        guard,
        "_run_workspace_binding_policy",
        lambda payload: events.append("workspace") or 0,
    )
    payload = {
        "tool_name": "kanban_create",
        "tool_input": {
            "title": "candidate A",
            "assignee": "kanban-investigator",
            "idempotency_key": "candidate-a",
        },
    }
    assert guard._run_specialist_policies(payload) == 0
    assert events == ["admit", "materialize", "workspace"]
