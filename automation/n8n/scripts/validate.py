#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
N8N = ROOT / "automation" / "n8n"

sys.path.insert(0, str(N8N / "scripts"))

from render_workflows import (
    EDGE_SYNC_CREDENTIAL_NAME,
    EDGE_SYNC_ACTUATOR_URL,
    EDGE_SYNC_TIMEOUT_MS,
    EDGE_SYNC_WEBHOOK_PATH,
    EDGE_SYNC_WORKFLOWS,
    EDGE_SYNC_WORKFLOW_ID,
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_edge_sync(workflow: dict[str, str]) -> None:
    path = N8N / "workflows" / f"{workflow['slug']}.json"
    data = load(path)

    assert data["id"] == EDGE_SYNC_WORKFLOW_ID
    assert data["name"] == "Hermes Webhook · GitHub PR edge sync"
    assert data["active"] is False

    nodes = {node["name"]: node for node in data["nodes"]}
    assert set(nodes) == {
        "GitHub edge sync webhook",
        "Normalize bounded event",
        "Allowed edge event?",
        "Run edge sync actuator",
        "Ignore unsupported event",
    }

    serialized = json.dumps(data, ensure_ascii=False)
    assert "scheduleTrigger" not in serialized
    assert "Schedule Trigger" not in serialized
    assert "/fallback" not in serialized
    assert "/api/cron/jobs/" not in serialized
    assert "127.0.0.1:5680" not in serialized
    assert "Execute Command" not in serialized

    webhook = nodes["GitHub edge sync webhook"]
    assert webhook["type"] == "n8n-nodes-base.webhook"
    assert webhook["typeVersion"] == 2.1
    assert webhook["parameters"] == {
        "httpMethod": "POST",
        "path": EDGE_SYNC_WEBHOOK_PATH,
        "authentication": "headerAuth",
        "responseMode": "lastNode",
        "options": {},
    }
    assert webhook["credentials"]["httpHeaderAuth"]["id"] == "REPLACE_AFTER_IMPORT"
    assert webhook["credentials"]["httpHeaderAuth"]["name"] == EDGE_SYNC_CREDENTIAL_NAME

    normalize = nodes["Normalize bounded event"]
    assert normalize["type"] == "n8n-nodes-base.set"
    assert {
        assignment["name"]
        for assignment in normalize["parameters"]["assignments"]["assignments"]
    } == {"repository", "event", "action", "merged", "label", "delivery"}
    assert normalize["parameters"]["includeOtherFields"] is False

    gate = nodes["Allowed edge event?"]
    assert gate["type"] == "n8n-nodes-base.if"
    condition = gate["parameters"]["conditions"]["conditions"][0]
    assert "pull_request" in condition["leftValue"]
    assert "closed" in condition["leftValue"]
    assert "merged" in condition["leftValue"]
    assert "agent-rework" in condition["leftValue"]
    assert len(data["connections"]["Allowed edge event?"]["main"]) == 2

    actuator = nodes["Run edge sync actuator"]
    assert actuator["type"] == "n8n-nodes-base.httpRequest"
    assert actuator["parameters"]["method"] == "POST"
    assert actuator["parameters"]["url"] == EDGE_SYNC_ACTUATOR_URL
    assert actuator["parameters"]["authentication"] == "genericCredentialType"
    assert actuator["parameters"]["genericAuthType"] == "httpHeaderAuth"
    assert actuator["parameters"]["sendBody"] is True
    assert actuator["parameters"]["specifyBody"] == "json"
    assert actuator["parameters"]["jsonBody"] == "={{ JSON.stringify($json) }}"
    assert actuator["parameters"]["options"]["timeout"] == EDGE_SYNC_TIMEOUT_MS
    assert actuator["credentials"]["httpHeaderAuth"]["id"] == "REPLACE_AFTER_IMPORT"
    assert actuator["credentials"]["httpHeaderAuth"]["name"] == EDGE_SYNC_CREDENTIAL_NAME

    ignored = nodes["Ignore unsupported event"]
    assert ignored["type"] == "n8n-nodes-base.set"
    ignored_names = {
        assignment["name"]
        for assignment in ignored["parameters"]["assignments"]["assignments"]
    }
    assert {"ok", "ignored", "reason"} <= ignored_names
    assert ignored["parameters"]["includeOtherFields"] is False


def validate_direct_intake_boundary() -> None:
    actuator_path = (
        ROOT
        / "automation"
        / "hermes"
        / "actuator"
        / "github_intake_actuator.py"
    )

    actuator = actuator_path.read_text(encoding="utf-8")

    assert 'HOST = "127.0.0.1"' in actuator
    assert "PORT = 5682" in actuator
    assert '"/v1/intake"' in actuator
    assert '"/v1/edge-sync"' in actuator
    assert 'PYTHON_BIN = Path("/opt/venv/bin/python3")' in actuator
    assert (
        '"/home/hermes/.hermes/scripts/'
        'github-agent-ready-kanban-intake.py"'
        in actuator
    )
    assert "shell=False" in actuator
    assert "_RUN_LOCK" in actuator
    assert "hmac.compare_digest" in actuator
    assert "--board" in actuator
    assert "--json" in actuator
    assert "_resolve_board" in actuator
    assert "EDGE_SYNC_SCRIPT" in actuator

    # There must be no caller-controlled command API.
    assert "shell=True" not in actuator
    assert "request_command" not in actuator
    assert "request_argv" not in actuator

    controller = (N8N / "lease-controller" / "controller.py").read_text(
        encoding="utf-8"
    )
    assert "_call_actuator(authorization)" in controller
    assert "/v1/intake" in controller
    assert "/api/cron/jobs/" not in controller
    assert "_call_hermes" not in controller


def main() -> int:
    assert len(EDGE_SYNC_WORKFLOWS) == 1
    edge_workflow = EDGE_SYNC_WORKFLOWS[0]
    assert edge_workflow["slug"] == "github-pr-edge-sync"

    assert [p.name for p in sorted((N8N / "workflows").glob("schedule-*.json"))] == []
    assert [p.name for p in sorted((N8N / "workflows").glob("github-*.json"))] == [
        "github-pr-edge-sync.json"
    ]

    validate_edge_sync(edge_workflow)
    validate_direct_intake_boundary()

    compose = yaml.safe_load((N8N / "compose.yaml").read_text(encoding="utf-8"))
    n8n = compose["services"]["n8n"]
    assert n8n["restart"] == "unless-stopped"
    assert n8n["network_mode"] == "host"
    assert "ports" not in n8n
    assert n8n["environment"]["N8N_LISTEN_ADDRESS"] == "127.0.0.1"
    assert n8n["environment"]["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "1"

    lease_env = compose["services"]["lease-controller"]["environment"]
    assert lease_env["LEASE_LISTEN_PORT"] == "5680"
    assert lease_env["LEASE_ACTUATOR_BASE_URL"] == "http://127.0.0.1:5682"
    assert lease_env["LEASE_TOKEN_FILE"] == "/state/secrets/hermes-intake-control-token"
    assert "LEASE_HERMES_BASE_URL" not in lease_env
    assert "LEASE_HERMES_JOB_ID" not in lease_env
    assert "LEASE_HERMES_PROFILE" not in lease_env

    router_env = compose["services"]["github-router"]["environment"]
    assert router_env["GITHUB_ROUTER_LISTEN_PORT"] == "5681"
    assert router_env["GITHUB_ROUTER_LEASE_BASE_URL"] == "http://127.0.0.1:5680"
    assert router_env["GITHUB_ROUTER_INTAKE_TOKEN_FILE"] == "/run/secrets/hermes-intake-control-token"
    assert (
        router_env["GITHUB_ROUTER_N8N_EDGE_SYNC_URL"]
        == "http://127.0.0.1:5678/webhook/hermes-github-edge-sync"
    )

    print(
        json.dumps(
            {
                "ok": True,
                "schedule_workflows": 0,
                "edge_sync_workflows": 1,
                "github_workflows": 1,
                "github_event_router": 1,
                "edge_sync_execution": "n8n-webhook->direct-actuator:5682",
                "hermes_cron_required": False,
                "hermes_schedule_owned_by_n8n": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
