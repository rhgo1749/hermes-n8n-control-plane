#!/usr/bin/env python3
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

    nodes = {
        node["name"]: node
        for node in data["nodes"]
    }

    assert set(nodes) == {
        "Schedule Trigger",
        "Run registry fallback",
    }

    schedule = nodes["Schedule Trigger"]
    assert schedule["type"] == "n8n-nodes-base.scheduleTrigger"
    assert schedule["parameters"]["rule"]["interval"] == [
        {
            "field": "cronExpression",
            "expression": "0 * * * *",
        }
    ]

    fallback = nodes["Run registry fallback"]
    assert fallback["type"] == "n8n-nodes-base.httpRequest"
    assert fallback["parameters"]["url"] == ROUTER_FALLBACK_URL
    assert (
        fallback["parameters"]["authentication"]
        == "genericCredentialType"
    )
    assert (
        fallback["parameters"]["genericAuthType"]
        == "httpHeaderAuth"
    )
    assert (
        fallback["parameters"]["options"]["timeout"]
        == FALLBACK_TIMEOUT_MS
    )

    serialized = json.dumps(data)

    # n8n never owns or directly calls Hermes execution.
    assert "/api/cron/jobs/" not in serialized
    assert "127.0.0.1:5680" not in serialized
    assert "credentials" not in serialized.lower()


def validate_dashboard_url_policy() -> None:
    # render_workflows still accepts the historical private-dashboard
    # argument for deterministic rendering compatibility. The rendered
    # fallback no longer calls that dashboard.
    assert (
        normalize_dashboard_url("https://n8n.example.com")
        == "https://n8n.example.com"
    )
    assert (
        normalize_dashboard_url("http://100.107.12.90:9119")
        == "http://100.107.12.90:9119"
    )

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

        raise AssertionError(
            f"accepted unsafe dashboard URL: {value}"
        )


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

    assert (
        'PYTHON_BIN = Path("/opt/venv/bin/python3")'
        in actuator
    )
    assert (
        '"/home/hermes/.hermes/scripts/'
        'github-agent-ready-kanban-intake.py"'
        in actuator
    )

    assert "shell=False" in actuator
    assert "_RUN_LOCK" in actuator
    assert "hmac.compare_digest" in actuator

    # There must be no caller-controlled command API.
    assert "shell=True" not in actuator
    assert "request_command" not in actuator
    assert "request_argv" not in actuator

    controller = (
        N8N
        / "lease-controller"
        / "controller.py"
    ).read_text(encoding="utf-8")

    assert "_call_actuator(authorization)" in controller
    assert "/v1/intake" in controller
    assert "/api/cron/jobs/" not in controller
    assert "_call_hermes" not in controller


def main() -> int:
    assert len(ACTIVE_JOBS) == 1
    fallback_job = ACTIVE_JOBS[0]

    assert fallback_job["slug"] == "github-agent-ready-intake"
    assert fallback_job["schedule"] == "0 * * * *"

    assert [
        p.name
        for p in sorted(
            (N8N / "workflows").glob("schedule-*.json")
        )
    ] == [
        "schedule-github-agent-ready-intake.json"
    ]

    assert sorted(
        (N8N / "workflows").glob("github-*-intake.json")
    ) == []

    validate_fallback(fallback_job)
    validate_dashboard_url_policy()
    validate_direct_intake_boundary()

    compose = yaml.safe_load(
        (N8N / "compose.yaml").read_text(encoding="utf-8")
    )

    n8n = compose["services"]["n8n"]

    assert n8n["restart"] == "unless-stopped"
    assert n8n["network_mode"] == "host"
    assert "ports" not in n8n
    assert (
        n8n["environment"]["N8N_LISTEN_ADDRESS"]
        == "127.0.0.1"
    )
    assert (
        n8n["environment"]["N8N_CONCURRENCY_PRODUCTION_LIMIT"]
        == "1"
    )

    lease = compose["services"]["lease-controller"]
    lease_env = lease["environment"]

    assert lease_env["LEASE_LISTEN_PORT"] == "5680"
    assert (
        lease_env["LEASE_ACTUATOR_BASE_URL"]
        == "http://127.0.0.1:5682"
    )
    assert (
        lease_env["LEASE_TOKEN_FILE"]
        == "/state/secrets/hermes-intake-control-token"
    )

    assert "LEASE_HERMES_BASE_URL" not in lease_env
    assert "LEASE_HERMES_JOB_ID" not in lease_env
    assert "LEASE_HERMES_PROFILE" not in lease_env

    router = compose["services"]["github-router"]
    router_env = router["environment"]

    assert router_env["GITHUB_ROUTER_LISTEN_PORT"] == "5681"
    assert (
        router_env["GITHUB_ROUTER_LEASE_BASE_URL"]
        == "http://127.0.0.1:5680"
    )
    assert (
        router_env["GITHUB_ROUTER_INTAKE_TOKEN_FILE"]
        == "/run/secrets/hermes-intake-control-token"
    )

    print(
        json.dumps(
            {
                "ok": True,
                "schedule_workflows": 1,
                "fallback_schedule": "hourly",
                "github_workflows": 0,
                "github_event_router": 1,
                "intake_execution": "direct-actuator:5682",
                "hermes_cron_required": False,
                "hermes_schedule_owned_by_n8n": False,
            }
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
