#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "automation/hermes/scripts/github-agent-ready-kanban-intake.py"
spec = importlib.util.spec_from_file_location("intake_full_fallback_isolation_test", SOURCE)
assert spec is not None and spec.loader is not None
intake = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = intake
spec.loader.exec_module(intake)


def config(repository: str) -> Any:
    name = repository.split("/", 1)[1]
    return intake.RepositoryConfig(
        name=repository,
        board=name.casefold(),
        checkout=f"/ws/projects/{name.casefold()}",
        default_branch="main",
        contract_paths=("AGENTS.md",),
        display_name=name,
    )


def test_full_fallback_isolates_unrecoverable_repository(capsys, monkeypatch):
    blocked = config("acme/blocked")
    healthy = config("acme/healthy")
    configs = (blocked, healthy)
    scope = intake.WakeScope(
        mode="full",
        repositories=(),
        expires_at=9_999_999_999,
        scope_id="scope-full",
        claim_token="a" * 32,
    )
    synced: list[str] = []
    created: list[str] = []
    acknowledged: list[str] = []

    monkeypatch.setattr(intake, "_github_token", lambda: "token")
    monkeypatch.setattr(intake, "_claim_wake_scope", lambda: scope)
    monkeypatch.setattr(intake, "_load_registry_snapshot", lambda token: {"repositories": []})
    monkeypatch.setattr(
        intake,
        "_repository_configs_from_registry",
        lambda snapshot, repository, **kwargs: (configs, []),
    )
    monkeypatch.setattr(intake, "_provision_bootstrap_boards", lambda *args, **kwargs: [])
    monkeypatch.setattr(intake, "_run_closed_issue_cleanup", lambda *args, **kwargs: [])
    monkeypatch.setattr(intake, "_board_slugs", lambda: {"blocked", "healthy"})
    monkeypatch.setattr(
        intake,
        "_issue_candidates",
        lambda token, fixture, selected: [
            (blocked, {"number": 1, "title": "blocked"}),
            (healthy, {"number": 2, "title": "healthy"}),
        ],
    )

    def snapshot(repository_config):
        if repository_config.name == blocked.name:
            raise intake.IntakeError("checkout_dirty")
        return intake.RepoSnapshot("a" * 40, "origin", ("AGENTS.md",))

    monkeypatch.setattr(intake, "_repo_snapshot", snapshot)
    monkeypatch.setattr(intake, "_closing_merged_pr_numbers", lambda *args: ())

    def create_task(repository_config, issue, snapshot_value, imported_at, *, tick_started):
        created.append(repository_config.name)
        return {"repository": repository_config.name, "issue": issue["number"]}

    monkeypatch.setattr(intake, "_create_task", create_task)

    def sync_board(repository_config, token, *, dry_run):
        synced.append(repository_config.name)
        return []

    monkeypatch.setattr(intake, "_sync_board", sync_board)
    monkeypatch.setattr(intake, "_telegram_config", lambda: None)
    monkeypatch.setattr(
        intake,
        "_ack_wake_scope",
        lambda claimed: acknowledged.append(claimed.scope_id),
    )

    result = intake._run_once(
        argparse.Namespace(
            dry_run=False,
            fixture_json=None,
            repository=None,
        )
    )

    assert result == 0
    assert created == [healthy.name]
    assert synced == [healthy.name]
    assert acknowledged == [scope.scope_id]
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["upserted_count"] == 1
    assert payload["results"] == [{"repository": healthy.name, "issue": 2}]
    assert payload["repository_outcomes"] == [
        {
            "repository": blocked.name,
            "action": "skipped",
            "reason": "checkout_dirty",
        },
        {
            "repository": healthy.name,
            "action": "created",
            "reason": "task_upserted",
        },
    ]
