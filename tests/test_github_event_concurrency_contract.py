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
    router_env = router["environment"]
    assert router_env["GITHUB_ROUTER_LISTEN_PORT"] == "5681"
    assert router_env["GITHUB_ROUTER_LEASE_BASE_URL"] == "http://127.0.0.1:5680"
    assert (
        router_env["GITHUB_ROUTER_N8N_EDGE_SYNC_URL"]
        == "http://127.0.0.1:5678/webhook/hermes-github-edge-sync"
    )

    assert sorted(WORKFLOWS.glob("schedule-*.json")) == []
    github_paths = sorted(WORKFLOWS.glob("github-*.json"))
    assert [path.name for path in github_paths] == ["github-pr-edge-sync.json"]

    workflow = json.loads(github_paths[0].read_text(encoding="utf-8"))
    assert workflow["name"] == "Hermes Webhook · GitHub PR edge sync"
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assert nodes["GitHub edge sync webhook"]["type"] == "n8n-nodes-base.webhook"
    assert nodes["GitHub edge sync webhook"]["parameters"]["path"] == "hermes-github-edge-sync"
    assert nodes["Normalize bounded event"]["parameters"]["includeOtherFields"] is False
    assert nodes["Run edge sync actuator"]["parameters"]["url"] == "http://127.0.0.1:5682/v1/edge-sync"
    assert nodes["Run edge sync actuator"]["parameters"]["jsonBody"] == "={{ JSON.stringify($json) }}"
    gate = nodes["Allowed edge event?"]["parameters"]["conditions"]["conditions"][0]
    assert "action === 'closed'" in gate["leftValue"]
    assert "merged === true" in gate["leftValue"]
    assert "label === 'agent-rework'" in gate["leftValue"]
    serialized = json.dumps(workflow)
    assert "scheduleTrigger" not in serialized
    assert "Schedule Trigger" not in serialized
    assert "/fallback" not in serialized
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
    assert '"/webhook/hermes-github-edge-sync"' in router_source
    assert "_normalise_pull_request_event" in router_source
    assert '"/fallback"' in router_source
    assert '"/reconcile"' in router_source

    actuator_source = (
        ROOT / "automation" / "hermes" / "actuator" / "github_intake_actuator.py"
    ).read_text(encoding="utf-8")
    assert '"/v1/edge-sync"' in actuator_source
    assert "_resolve_board" in actuator_source
    assert "--board" in actuator_source
    assert "--json" in actuator_source
    assert "shell=False" in actuator_source

    controller_source = (N8N / "lease-controller" / "controller.py").read_text(encoding="utf-8")
    assert "_call_actuator(authorization)" in controller_source
    assert "/v1/intake" in controller_source
    assert "/api/cron/jobs/" not in controller_source
    assert "_call_hermes" not in controller_source

    print(
        json.dumps(
            {
                "ok": True,
                "github_event_workflows": 1,
                "schedule_workflows": 0,
                "edge_sync_workflow": "webhook-filter-actuator",
                "github_event_router": 1,
                "production_concurrency_limit": 1,
                "production_concurrency_limit_role": "load-limiter",
                "stale_pause_guard": "lease-controller",
                "scope_handoff": "durable-fifo-queue-for-intake-events",
                "edge_sync_execution": "direct-actuator:5682",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
