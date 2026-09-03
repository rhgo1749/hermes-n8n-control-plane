from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "automation" / "n8n" / "github-router" / "router_entrypoint.py"
COMPOSE = ROOT / "automation" / "n8n" / "compose.yaml"


def _load_entrypoint():
    name = "test_github_router_completion_comment_entrypoint"
    spec = importlib.util.spec_from_file_location(name, ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fake_core(*, managed: bool = True):
    core = ModuleType("fake_router_core")
    calls: dict[str, list[object]] = {"scope": [], "edge": []}

    def event_repositories(event, payload):
        repository = payload.get("repository", {}).get("full_name")
        return [repository] if repository else []

    def enqueue_scope(**kwargs):
        calls["scope"].append(dict(kwargs))
        return {"id": "scope-1", "mode": "event"}

    def managed_repository(repository):
        return managed and repository == "rhgo1749/ctrl-hangul"

    def n8n_edge_sync(event):
        calls["edge"].append(dict(event))
        return {"status": 200, "body": {"ok": True}}

    core._event_repositories = event_repositories
    core._enqueue_scope = enqueue_scope
    core._managed_repository = managed_repository
    core._n8n_edge_sync = n8n_edge_sync
    return core, calls


def _payload(body: str, *, action: str = "created", pull_request: bool = True):
    issue = {"number": 92}
    if pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/repos/rhgo1749/ctrl-hangul/pulls/92"}
    return {
        "action": action,
        "repository": {"full_name": "rhgo1749/ctrl-hangul"},
        "issue": issue,
        "comment": {"body": body},
    }


def test_completion_comment_gets_immediate_edge_wake_hint():
    entrypoint = _load_entrypoint()
    core, calls = _fake_core()
    entrypoint.install(core)

    repositories = core._event_repositories(
        "issue_comment",
        _payload("\nAGENT_REWORK_COMPLETE\ntask=t_1187aba8\nvalidation=passed\n"),
    )
    result = core._enqueue_scope(
        full=False,
        repositories=repositories,
        delivery_id="delivery-r10",
    )

    assert len(calls["scope"]) == 1
    assert calls["edge"] == [
        {
            "repository": "rhgo1749/ctrl-hangul",
            "event": "pull_request",
            "action": "labeled",
            "merged": False,
            "label": "agent-rework",
            "delivery": "delivery-r10",
        }
    ]
    assert result["completion_edge_sync"] is True
    assert result["completion_edge_sync_status"] == 200


def test_marker_is_wake_hint_only_when_first_nonempty_line_on_pr_created_comment():
    entrypoint = _load_entrypoint()

    cases = [
        ("issue_comment", _payload("ordinary discussion")),
        ("issue_comment", _payload("prefix\nAGENT_REWORK_COMPLETE\ntask=t_1187aba8")),
        ("issue_comment", _payload("AGENT_REWORK_COMPLETE", action="edited")),
        ("issue_comment", _payload("AGENT_REWORK_COMPLETE", pull_request=False)),
        ("issues", _payload("AGENT_REWORK_COMPLETE")),
    ]

    for event, payload in cases:
        core, calls = _fake_core()
        entrypoint.install(core)
        repositories = core._event_repositories(event, payload)
        core._enqueue_scope(
            full=False,
            repositories=repositories,
            delivery_id="delivery-normal",
        )
        assert calls["edge"] == []


def test_unknown_repository_keeps_onboarding_path_without_edge_wake():
    entrypoint = _load_entrypoint()
    core, calls = _fake_core(managed=False)
    entrypoint.install(core)

    repositories = core._event_repositories(
        "issue_comment", _payload("AGENT_REWORK_COMPLETE\ntask=t_1187aba8")
    )
    result = core._enqueue_scope(
        full=False,
        repositories=repositories,
        delivery_id="delivery-unknown",
    )

    assert result == {"id": "scope-1", "mode": "event"}
    assert calls["edge"] == []


def test_edge_wake_failure_is_not_swallowed_after_scope_persistence():
    entrypoint = _load_entrypoint()
    core, calls = _fake_core()

    def fail_edge(_event):
        raise RuntimeError("edge wake failed")

    core._n8n_edge_sync = fail_edge
    entrypoint.install(core)
    repositories = core._event_repositories(
        "issue_comment", _payload("AGENT_REWORK_COMPLETE\ntask=t_1187aba8")
    )

    with pytest.raises(RuntimeError, match="edge wake failed"):
        core._enqueue_scope(
            full=False,
            repositories=repositories,
            delivery_id="delivery-fail",
        )
    assert len(calls["scope"]) == 1


def test_compose_runs_router_overlay_and_mounts_both_sources():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "- /app/router_entrypoint.py" in text
    assert "./github-router/router.py:/app/router.py:ro" in text
    assert "./github-router/router_entrypoint.py:/app/router_entrypoint.py:ro" in text
