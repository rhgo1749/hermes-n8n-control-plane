#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
N8N = ROOT / "automation" / "n8n"
WORKFLOWS = N8N / "workflows"


def main() -> int:
    compose = yaml.safe_load(
        (N8N / "compose.yaml").read_text(encoding="utf-8")
    )
    environment = compose["services"]["n8n"]["environment"]
    assert environment["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "1"

    lease = compose["services"]["lease-controller"]
    assert lease["environment"]["LEASE_LISTEN_PORT"] == "5680"
    assert lease["environment"]["LEASE_HERMES_JOB_ID"] == "bf431b2a6ba6"
    assert lease["environment"]["LEASE_HERMES_PROFILE"] == "default"

    router = compose["services"]["github-router"]
    assert router["environment"]["GITHUB_ROUTER_LISTEN_PORT"] == "5681"
    assert (
        router["environment"]["GITHUB_ROUTER_LEASE_BASE_URL"]
        == "http://127.0.0.1:5680"
    )

    # The production intake is event-driven.  No tracked n8n workflow may
    # recreate the retired five-minute polling schedule or a parallel GitHub
    # Trigger path.
    github_paths = sorted(WORKFLOWS.glob("github-*-intake.json"))
    schedule_paths = sorted(WORKFLOWS.glob("schedule-*.json"))
    assert github_paths == []
    assert schedule_paths == []

    router_source = (N8N / "github-router" / "router.py").read_text(encoding="utf-8")
    assert "_enqueue_scope" in router_source
    assert "_claim_scope" in router_source
    assert "X-Hub-Signature-256" in router_source
    assert '"/github/hermes-intake"' in router_source
    assert '"/fallback"' in router_source
    assert '"/reconcile"' in router_source

    controller_source = (N8N / "lease-controller" / "controller.py").read_text(
        encoding="utf-8"
    )
    assert 'f"{HERMES_JOB_ID}/{action}?profile={HERMES_PROFILE}"' in controller_source
    assert '_call_hermes("trigger", authorization)' in controller_source
    assert '_call_hermes("pause", authorization)' in controller_source

    print(
        json.dumps(
            {
                "ok": True,
                "github_event_workflows": 0,
                "schedule_workflows": 0,
                "github_event_router": 1,
                "production_concurrency_limit": 1,
                "production_concurrency_limit_role": "load-limiter",
                "stale_pause_guard": "lease-controller",
                "scope_handoff": "durable-fifo-queue",
                "hermes_job_preserved": "default:bf431b2a6ba6",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
