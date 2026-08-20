#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
N8N = ROOT / "automation" / "n8n"
WORKFLOWS = N8N / "workflows"


def main() -> int:
    compose = yaml.safe_load((N8N / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["n8n"]["environment"]
    assert environment["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "1"

    lease = compose["services"]["lease-controller"]
    assert lease["environment"]["LEASE_LISTEN_PORT"] == "5680"
    assert lease["environment"]["LEASE_ACTUATOR_BASE_URL"] == "http://" + "127.0.0.1:5682"
    assert lease["environment"]["LEASE_TOKEN_FILE"] == "/state/secrets/hermes-intake-control-token"
    assert "LEASE_HERMES_JOB_ID" not in lease["environment"]
    assert "LEASE_HERMES_PROFILE" not in lease["environment"]

    router = compose["services"]["github-router"]
    assert router["environment"]["GITHUB_ROUTER_LISTEN_PORT"] == "5681"
    assert router["environment"]["GITHUB_ROUTER_LEASE_BASE_URL"] == "http://127.0.0.1:5680"

    github_paths = sorted(WORKFLOWS.glob("github-*-intake.json"))
    schedule_paths = sorted(WORKFLOWS.glob("schedule-*.json"))
    assert github_paths == []
    assert [path.name for path in schedule_paths] == ["schedule-github-agent-ready-intake.json"]

    workflow = json.loads(schedule_paths[0].read_text(encoding="utf-8"))
    assert workflow["name"] == "Hermes fallback · GitHub Kanban intake"
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assert nodes["Schedule Trigger"]["parameters"]["rule"]["interval"] == [
        {"field": "cronExpression", "expression": "0 * * * *"}
    ]
    assert nodes["Run registry fallback"]["parameters"]["url"] == "http://127.0.0.1:5681/fallback"
    serialized = json.dumps(workflow)
    assert "/api/cron/jobs/" not in serialized
    assert "127.0.0.1:5680" not in serialized

    router_source = (N8N / "github-router" / "router.py").read_text(encoding="utf-8")
    assert "_enqueue_scope" in router_source
    assert "_claim_scope" in router_source
    assert "X-Hub-Signature-256" in router_source
    assert "X-GitHub-Delivery" in router_source
    assert "_claim_delivery" in router_source
    assert "delivery_dedupe" in router_source
    assert '"/github/hermes-intake"' in router_source
    assert '"/fallback"' in router_source
    assert '"/reconcile"' in router_source

    controller_source = (N8N / "lease-controller" / "controller.py").read_text(encoding="utf-8")
    assert "_call_actuator(authorization)" in controller_source
    assert "/v1/intake" in controller_source
    assert "/api/cron/jobs/" not in controller_source
    assert "_call_hermes" not in controller_source

    print(json.dumps({
        "ok": True,
        "github_event_workflows": 0,
        "schedule_workflows": 1,
        "fallback_schedule": "hourly",
        "github_event_router": 1,
        "production_concurrency_limit": 1,
        "production_concurrency_limit_role": "load-limiter",
        "stale_pause_guard": "lease-controller",
        "scope_handoff": "durable-fifo-queue",
        "intake_execution": "direct-actuator:5682",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
