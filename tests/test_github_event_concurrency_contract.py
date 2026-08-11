#!/usr/bin/env python3
"""Regression contract for serialized GitHub event intake executions.

Five GitHub trigger workflows and the temporary five-minute polling fallback all
control the same Hermes intake job.  Their trigger -> wait -> pause sequences
must never overlap, otherwise an older delayed pause can disable a newer wake.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
N8N = ROOT / "automation" / "n8n"
WORKFLOWS = N8N / "workflows"
INTAKE_ID = "bf431b2a6ba6"
WAIT_SECONDS = 75


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def linear_chain(workflow: dict) -> list[str]:
    """Return the single main-output chain starting at the trigger node."""
    nodes = {node["name"]: node for node in workflow["nodes"]}
    trigger_names = [
        name
        for name, node in nodes.items()
        if node["type"] in {"n8n-nodes-base.githubTrigger", "n8n-nodes-base.scheduleTrigger"}
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


def assert_intake_sequence(workflow: dict) -> None:
    nodes = {node["name"]: node for node in workflow["nodes"]}
    chain = linear_chain(workflow)
    assert len(chain) == 4, chain
    assert nodes[chain[1]]["type"] == "n8n-nodes-base.httpRequest"
    assert nodes[chain[2]]["type"] == "n8n-nodes-base.wait"
    assert nodes[chain[3]]["type"] == "n8n-nodes-base.httpRequest"

    trigger_url = nodes[chain[1]]["parameters"]["url"]
    pause_url = nodes[chain[3]]["parameters"]["url"]
    assert f"/api/cron/jobs/{INTAKE_ID}/trigger?profile=default" in trigger_url
    assert f"/api/cron/jobs/{INTAKE_ID}/pause?profile=default" in pause_url
    assert nodes[chain[2]]["parameters"] == {
        "resume": "timeInterval",
        "amount": WAIT_SECONDS,
        "unit": "seconds",
    }


def stale_pause_exists(operations: list[tuple[str, str]]) -> bool:
    """Detect a pause from one execution after another execution has triggered."""
    latest_trigger: str | None = None
    for execution, action in operations:
        if action == "trigger":
            latest_trigger = execution
        elif action == "pause" and latest_trigger not in {None, execution}:
            return True
    return False


def main() -> int:
    compose = yaml.safe_load((N8N / "compose.yaml").read_text(encoding="utf-8"))
    environment = compose["services"]["n8n"]["environment"]
    assert environment["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "1"

    github_paths = sorted(WORKFLOWS.glob("github-*-intake.json"))
    assert len(github_paths) == 5
    for path in github_paths:
        workflow = load(path)
        assert workflow["active"] is False
        assert_intake_sequence(workflow)

    schedule_paths = sorted(WORKFLOWS.glob("schedule-*.json"))
    assert [path.name for path in schedule_paths] == ["schedule-github-agent-ready-intake.json"]
    assert_intake_sequence(load(schedule_paths[0]))

    # This is the bug the production concurrency guard prevents.
    assert stale_pause_exists(
        [("repo-a", "trigger"), ("repo-b", "trigger"), ("repo-a", "pause"), ("repo-b", "pause")]
    )

    # With one production slot, an execution completes its 75-second wait and
    # pause before the next event execution starts: no older pause can overtake
    # a newer trigger.
    serialized = [
        ("repo-a", "trigger"),
        ("repo-a", "pause"),
        ("repo-b", "trigger"),
        ("repo-b", "pause"),
        ("fallback", "trigger"),
        ("fallback", "pause"),
    ]
    assert not stale_pause_exists(serialized)

    print(
        json.dumps(
            {
                "ok": True,
                "github_event_workflows": len(github_paths),
                "production_concurrency_limit": 1,
                "stale_pause_regression": "covered",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
