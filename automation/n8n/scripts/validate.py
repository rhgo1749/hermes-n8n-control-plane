#!/usr/bin/env python3
"""Static regression checks for n8n templates, Compose hardening, and allowlists."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
N8N = ROOT / "automation" / "n8n"
sys.path.insert(0, str(N8N / "scripts"))
from render_workflows import (  # noqa: E402
    ACTIVE_JOBS,
    GITHUB_REPOSITORIES,
    INTAKE_JOB,
    PAUSE_TIMEOUT_MS,
    PLACEHOLDER_URL,
    TRIGGER_TIMEOUT_MS,
    WAIT_SECONDS,
    normalize_dashboard_url,
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def http_urls(workflow: dict) -> list[str]:
    return [
        node["parameters"]["url"]
        for node in workflow["nodes"]
        if node["type"] == "n8n-nodes-base.httpRequest"
    ]


def assert_compensating_http_nodes(nodes: dict[str, dict], trigger_name: str, pause_name: str) -> None:
    trigger = nodes[trigger_name]
    pause = nodes[pause_name]
    trigger_response = trigger["parameters"]["options"]["response"]["response"]
    pause_response = pause["parameters"]["options"]["response"]["response"]
    assert trigger["parameters"]["options"]["timeout"] == TRIGGER_TIMEOUT_MS
    assert trigger_response["neverError"] is True
    assert pause["parameters"]["options"]["timeout"] == PAUSE_TIMEOUT_MS
    assert pause_response["neverError"] is False


def validate_schedule(job: dict[str, str]) -> None:
    path = N8N / "workflows" / f"schedule-{job['slug']}.json"
    data = load(path)
    assert data["active"] is False
    nodes = {node["name"]: node for node in data["nodes"]}
    schedule = nodes["Schedule Trigger"]
    assert schedule["type"] == "n8n-nodes-base.scheduleTrigger"
    rule = schedule["parameters"]["rule"]["interval"]
    assert rule == [{"field": "cronExpression", "expression": job["schedule"]}]
    wait = nodes["Wait for Hermes ticker"]
    assert wait["parameters"] == {"resume": "timeInterval", "amount": WAIT_SECONDS, "unit": "seconds"}
    assert_compensating_http_nodes(
        nodes,
        "Trigger existing Hermes cron job",
        "Pause legacy Hermes schedule",
    )
    expected = {
        f"{PLACEHOLDER_URL}/api/cron/jobs/{job['id']}/trigger?profile={job['profile']}",
        f"{PLACEHOLDER_URL}/api/cron/jobs/{job['id']}/pause?profile={job['profile']}",
    }
    assert set(http_urls(data)) == expected
    assert "credentials" not in json.dumps(data).lower()


def validate_github(repo: dict[str, str]) -> None:
    path = N8N / "workflows" / f"github-{repo['slug']}-intake.json"
    data = load(path)
    assert data["active"] is False
    nodes = {node["name"]: node for node in data["nodes"]}
    github = nodes["GitHub Trigger"]
    assert github["type"] == "n8n-nodes-base.githubTrigger"
    parameters = github["parameters"]
    assert parameters["owner"]["value"] == repo["owner"]
    assert parameters["repository"]["value"] == repo["repository"]
    assert parameters["events"] == ["issues", "issue_comment", "pull_request"]
    assert_compensating_http_nodes(
        nodes,
        "Trigger existing Hermes intake",
        "Pause legacy Hermes intake schedule",
    )
    expected = {
        f"{PLACEHOLDER_URL}/api/cron/jobs/{INTAKE_JOB['id']}/trigger?profile=default",
        f"{PLACEHOLDER_URL}/api/cron/jobs/{INTAKE_JOB['id']}/pause?profile=default",
    }
    assert set(http_urls(data)) == expected
    assert "credentials" not in json.dumps(data).lower()


def validate_dashboard_url_policy() -> None:
    assert normalize_dashboard_url("https://n8n.example.com") == "https://n8n.example.com"
    assert normalize_dashboard_url("http://100.107.12.90:9119") == "http://100.107.12.90:9119"
    for value in ("http://example.com", "http://8.8.8.8", "http://user:password@100.107.12.90"):
        try:
            normalize_dashboard_url(value)
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe dashboard URL: {value}")


def main() -> int:
    for job in ACTIVE_JOBS:
        validate_schedule(job)
    for repo in GITHUB_REPOSITORIES:
        validate_github(repo)
    validate_dashboard_url_policy()

    compose_text = (N8N / "compose.yaml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    service = compose["services"]["n8n"]
    assert service["restart"] == "unless-stopped"
    assert service["ports"] == ["127.0.0.1:${N8N_HOST_PORT:-5678}:5678"]
    assert "/var/run/docker.sock" not in compose_text
    environment = service["environment"]
    assert environment["N8N_BLOCK_ENV_ACCESS_IN_NODE"] == "true"
    assert environment["N8N_DIAGNOSTICS_ENABLED"] == "false"
    assert environment["N8N_PUBLIC_API_DISABLED"] == "true"

    plugin = (ROOT / "hermes-plugin" / "n8n-cron-auth" / "__init__.py").read_text(encoding="utf-8")
    for job in ACTIVE_JOBS:
        assert job["id"] in plugin
    assert "create_job" not in plugin and "delete" not in plugin

    print(json.dumps({"ok": True, "schedule_workflows": len(ACTIVE_JOBS), "github_workflows": len(GITHUB_REPOSITORIES)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
