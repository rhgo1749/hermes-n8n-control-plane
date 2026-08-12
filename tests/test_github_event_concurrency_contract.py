#!/usr/bin/env python3
"""Regression contract for lease-guarded GitHub event intake executions.

GitHub event workflows and the five-minute polling fallback may overlap while
waiting. n8n's production concurrency limit is therefore only a load limiter.
Stale-pause correctness is provided by the loopback lease controller.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
N8N = ROOT / "automation" / "n8n"
WORKFLOWS = N8N / "workflows"

LEASE_TRIGGER_URL = "http://127.0.0.1:5680/trigger"
WAIT_SECONDS = 75


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def linear_chain(workflow: dict) -> list[str]:
    nodes = {node["name"]: node for node in workflow["nodes"]}
    trigger_names = [
        name
        for name, node in nodes.items()
        if node["type"]
        in {
            "n8n-nodes-base.githubTrigger",
            "n8n-nodes-base.scheduleTrigger",
        }
    ]
    assert len(trigger_names) == 1

    chain = [trigger_names[0]]
    current = trigger_names[0]

    while current in workflow["connections"]:
        outputs = workflow["connections"][current].get("main", [])
        if not outputs or not outputs[0]:
            break
        assert len(outputs[0]) == 1
        current = outputs[0][0]["node"]
        chain.append(current)

    return chain


def expected_pause_url(trigger_name: str) -> str:
    return (
        "={{ 'http://127.0.0.1:5680/pause?lease=' + "
        f"$('{trigger_name}').item.json.body.lease }}}}"
    )


def assert_intake_sequence(workflow: dict) -> None:
    nodes = {node["name"]: node for node in workflow["nodes"]}
    chain = linear_chain(workflow)

    assert len(chain) == 4, chain
    assert nodes[chain[1]]["type"] == "n8n-nodes-base.httpRequest"
    assert nodes[chain[2]]["type"] == "n8n-nodes-base.wait"
    assert nodes[chain[3]]["type"] == "n8n-nodes-base.httpRequest"

    trigger_name = chain[1]
    trigger_url = nodes[trigger_name]["parameters"]["url"]
    pause_url = nodes[chain[3]]["parameters"]["url"]

    assert trigger_url == LEASE_TRIGGER_URL
    assert pause_url == expected_pause_url(trigger_name)

    # Workflows must never bypass the lease controller for intake lifecycle
    # control. The controller alone forwards authenticated requests to Hermes.
    assert "/api/cron/jobs/" not in trigger_url
    assert "/api/cron/jobs/" not in pause_url

    assert nodes[chain[2]]["parameters"] == {
        "resume": "timeInterval",
        "amount": WAIT_SECONDS,
        "unit": "seconds",
    }


def stale_pause_exists(operations: list[tuple[str, str]]) -> bool:
    """Show why overlapping trigger/wait/pause sequences need a lease guard."""
    latest_trigger: str | None = None

    for execution, action in operations:
        if action == "trigger":
            latest_trigger = execution
        elif action == "pause" and latest_trigger not in {None, execution}:
            return True

    return False


def main() -> int:
    compose = yaml.safe_load(
        (N8N / "compose.yaml").read_text(encoding="utf-8")
    )

    environment = compose["services"]["n8n"]["environment"]

    # Kept as a burst/load limiter, not as the stale-pause correctness guard.
    assert environment["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "1"

    lease = compose["services"]["lease-controller"]
    assert lease["network_mode"] == "host"
    assert lease["read_only"] is True
    assert lease["environment"]["LEASE_LISTEN_PORT"] == "5680"
    assert (
        lease["environment"]["LEASE_HERMES_JOB_ID"]
        == "bf431b2a6ba6"
    )
    assert lease["environment"]["LEASE_HERMES_PROFILE"] == "default"

    github_paths = sorted(WORKFLOWS.glob("github-*-intake.json"))
    assert len(github_paths) == 5

    for path in github_paths:
        workflow = load(path)
        assert workflow["active"] is False
        assert_intake_sequence(workflow)

    schedule_paths = sorted(WORKFLOWS.glob("schedule-*.json"))
    assert [path.name for path in schedule_paths] == [
        "schedule-github-agent-ready-intake.json"
    ]
    assert_intake_sequence(load(schedule_paths[0]))

    # n8n Wait executions really can overlap. This sequence is intentionally
    # stale and demonstrates why serialization cannot be the correctness model.
    assert stale_pause_exists(
        [
            ("repo-a", "trigger"),
            ("repo-b", "trigger"),
            ("repo-a", "pause"),
            ("repo-b", "pause"),
        ]
    )

    print(
        json.dumps(
            {
                "ok": True,
                "github_event_workflows": len(github_paths),
                "production_concurrency_limit": 1,
                "production_concurrency_limit_role": "load-limiter",
                "stale_pause_guard": "lease-controller",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
