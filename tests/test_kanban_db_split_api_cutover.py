#!/usr/bin/env python3
"""Prevent regressions onto Hermes' expiring kanban_db plugin-compat aliases."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (
    ROOT / "edge",
    ROOT / "tests",
    ROOT / "automation/hermes/scripts",
    ROOT / "hermes-plugin",
)
MOVED = {
    "connect_closing": "hermes_cli.kanban_db_connect",
    "check_respawn_guard": "hermes_cli.kanban_db_dispatch",
    "dispatch_once": "hermes_cli.kanban_db_dispatch",
    "resolve_workspace": "hermes_cli.kanban_db_workspace",
    "set_branch_name": "hermes_cli.kanban_db_workspace",
    "set_workspace_path": "hermes_cli.kanban_db_workspace",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        files.extend(path for path in root.rglob("*.py") if ".worktrees" not in path.parts)
    return sorted(files)


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    kanban_db_aliases: set[str] = set()
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "hermes_cli":
            for imported in node.names:
                if imported.name == "kanban_db":
                    kanban_db_aliases.add(imported.asname or imported.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "hermes_cli.kanban_db":
            for imported in node.names:
                if imported.name in MOVED:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}: import {imported.name} "
                        f"from {MOVED[imported.name]} instead"
                    )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in MOVED:
            continue
        if isinstance(node.value, ast.Name) and node.value.id in kanban_db_aliases:
            violations.append(
                f"{path.relative_to(ROOT)}:{node.lineno}: {node.value.id}.{node.attr} "
                f"must use {MOVED[node.attr]}.{node.attr}"
            )

    return violations


def test_no_expiring_kanban_db_plugin_compat_aliases() -> None:
    violations = [
        violation
        for path in _python_files()
        for violation in _violations(path)
    ]
    assert violations == [], "\n".join(violations)
