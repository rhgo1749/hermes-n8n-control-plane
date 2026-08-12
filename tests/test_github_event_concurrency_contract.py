#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
N8N = ROOT / "automation" / "n8n"
WORKFLOWS = N8N / "workflows"
ROUTER_FALLBACK_URL = "http://127.0.0.1:5681/fallback"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    compose = yaml.safe_load(
        (N8N / "compose.yaml").read_text(encoding="utf-8")
    )
    environment = compose["services"]["n8n"]["environment"]
    assert environment["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "1"
    lease = compose["services"]["lease-controller"]
    assert lease["environment"]["LEASE_LISTEN_PORT"] == "5680"
    router = compose["services"]["github-router"]
    assert router["environment"]["GITHUB_ROUTER_LISTEN_PORT"] == "5681"
    assert (
        router["environment"]["GITHUB_ROUTER_LEASE_BASE_URL"]
        == "http://127.0.0.1:5680"
    )
    github_paths = sorted(WORKFLOWS.glob("github-*-intake.json"))
    assert github_paths == []
    schedule_paths = sorted(WORKFLOWS.glob("schedule-*.json"))
    assert [path.name for path in schedule_paths] == [
        "schedule-github-agent-ready-intake.json"
    ]
    workflow = load(schedule_paths[0])
    nodes = {node["name"]: node for node in workflow["nodes"]}
    assert set(nodes) == {"Schedule Trigger", "Run registry fallback"}
    assert nodes["Run registry fallback"]["parameters"]["url"] == ROUTER_FALLBACK_URL
    serialized = json.dumps(workflow)
    assert "127.0.0.1:5680" not in serialized
    assert "/api/cron/jobs/" not in serialized
    router_source = (N8N / "github-router" / "router.py").read_text(encoding="utf-8")
    assert "_enqueue_scope" in router_source
    assert "_claim_scope" in router_source
    assert "X-Hub-Signature-256" in router_source
    print(
        json.dumps(
            {
                "ok": True,
                "github_event_workflows": 0,
                "github_event_router": 1,
                "production_concurrency_limit": 1,
                "production_concurrency_limit_role": "load-limiter",
                "stale_pause_guard": "lease-controller",
                "scope_handoff": "durable-fifo-queue",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
