#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
N8N = ROOT / "automation" / "n8n"
sys.path.insert(0, str(N8N / "scripts"))
from render_workflows import (  # noqa: E402
    ACTIVE_JOBS,
    FALLBACK_TIMEOUT_MS,
    ROUTER_FALLBACK_URL,
    normalize_dashboard_url,
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_fallback(job: dict[str, str]) -> None:
    path = N8N / "workflows" / f"schedule-{job['slug']}.json"
    data = load(path)
    assert data["name"] == "Hermes fallback · GitHub Kanban intake"
    assert data["active"] is False
    nodes = {node["name"]: node for node in data["nodes"]}
    assert set(nodes) == {"Schedule Trigger", "Run registry fallback"}
    schedule = nodes["Schedule Trigger"]
    assert schedule["type"] == "n8n-nodes-base.scheduleTrigger"
    assert schedule["parameters"]["rule"]["interval"] == [
        {"field": "cronExpression", "expression": "0 * * * *"}
    ]
    fallback = nodes["Run registry fallback"]
    assert fallback["type"] == "n8n-nodes-base.httpRequest"
    assert fallback["parameters"]["url"] == ROUTER_FALLBACK_URL
    assert fallback["parameters"]["authentication"] == "genericCredentialType"
    assert fallback["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert fallback["parameters"]["options"]["timeout"] == FALLBACK_TIMEOUT_MS
    serialized = json.dumps(data)
    assert "/api/cron/jobs/" not in serialized
    assert "127.0.0.1:5680" not in serialized
    assert "credentials" not in serialized.lower()


def validate_dashboard_url_policy() -> None:
    assert normalize_dashboard_url("https://n8n.example.com") == "https://n8n.example.com"
    assert normalize_dashboard_url("http://100.107.12.90:9119") == "http://100.107.12.90:9119"
    for value in (
        "http://example.com",
        "http://8.8.8.8",
        "http://user:password@100.107.12.90",
        "http://100.107.12.90:not-a-port",
        "http://100.107.12.90:9119/path",
        "http://100.107.12.90:9119?query=1",
        "http://100.107.12.90:9119#fragment",
    ):
        try:
            normalize_dashboard_url(value)
        except ValueError:
            continue
        raise AssertionError(f"accepted unsafe dashboard URL: {value}")


def validate_single_intake_boundary() -> None:
    expected_jobs = {"bf431b2a6ba6": "default"}
    plugin_source = (ROOT / "hermes-plugin" / "n8n-cron-auth" / "__init__.py").read_text(encoding="utf-8")
    plugin_tree = ast.parse(plugin_source)
    allowed_jobs = None
    for node in ast.walk(plugin_tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "ALLOWED_JOBS":
            assert node.value is not None
            allowed_jobs = ast.literal_eval(node.value)
            break
    assert allowed_jobs == expected_jobs
    installer_source = (N8N / "scripts" / "configure-hermes-service-auth.sh").read_text(encoding="utf-8")
    installer_match = re.search(r"(?ms)^expected = (?P<mapping>\{.*?^\})", installer_source)
    assert installer_match is not None
    assert ast.literal_eval(installer_match.group("mapping")) == expected_jobs
    assert "create_job" not in plugin_source
    assert "delete" not in plugin_source


def main() -> int:
    assert len(ACTIVE_JOBS) == 1
    job = ACTIVE_JOBS[0]
    assert job["id"] == "bf431b2a6ba6"
    assert job["profile"] == "default"
    assert job["schedule"] == "0 * * * *"
    assert [p.name for p in sorted((N8N / "workflows").glob("schedule-*.json"))] == [
        "schedule-github-agent-ready-intake.json"
    ]
    assert sorted((N8N / "workflows").glob("github-*-intake.json")) == []
    validate_fallback(job)
    validate_dashboard_url_policy()
    validate_single_intake_boundary()

    compose_text = (N8N / "compose.yaml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    n8n = compose["services"]["n8n"]
    assert n8n["restart"] == "unless-stopped"
    assert n8n["network_mode"] == "host"
    assert "ports" not in n8n
    assert n8n["environment"]["N8N_LISTEN_ADDRESS"] == "127.0.0.1"
    assert n8n["environment"]["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "1"

    lease = compose["services"]["lease-controller"]
    assert lease["environment"]["LEASE_LISTEN_PORT"] == "5680"
    assert lease["environment"]["LEASE_HERMES_JOB_ID"] == "bf431b2a6ba6"
    assert lease["environment"]["LEASE_HERMES_PROFILE"] == "default"

    router = compose["services"]["github-router"]
    assert router["environment"]["GITHUB_ROUTER_LISTEN_PORT"] == "5681"
    assert router["environment"]["GITHUB_ROUTER_LEASE_BASE_URL"] == "http://127.0.0.1:5680"

    print(json.dumps({
        "ok": True,
        "schedule_workflows": 1,
        "fallback_schedule": "hourly",
        "github_workflows": 0,
        "github_event_router": 1,
        "hermes_intake_job": "default:bf431b2a6ba6",
        "hermes_schedule_owned_by_n8n": False,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
