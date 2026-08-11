#!/usr/bin/env python3
"""Generate tracked n8n workflow templates and render host-specific imports.

The templates intentionally have no credentials and are inactive.  The operator
creates a Header Auth credential in n8n after importing, so a bearer token never
lands in a workflow JSON file or Git history.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"
PLACEHOLDER_URL = "__HERMES_DASHBOARD_URL__"
WAIT_SECONDS = 75
TRIGGER_TIMEOUT_MS = 10_000
PAUSE_TIMEOUT_MS = 60_000
_NAMESPACE = uuid.UUID("4d3669cd-39ce-4c84-a10d-762278d838c6")
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Exact migration inventory, taken from the live Hermes cron stores on
# 2026-08-11. Only active jobs are migrated. Existing paused jobs remain paused.
ACTIVE_JOBS: tuple[dict[str, str], ...] = (
    {
        "slug": "daily-session-cleanup",
        "profile": "default",
        "id": "168bd63461e7",
        "name": "매일 저가치 세션 정리",
        "schedule": "0 9 * * *",
    },
    {
        "slug": "cleanup-stale-feature-repos",
        "profile": "default",
        "id": "e432a90c1361",
        "name": "cleanup-stale-feature-repos",
        "schedule": "0 9 * * *",
    },
    {
        "slug": "repo-fetch-check",
        "profile": "default",
        "id": "df360bfa297d",
        "name": "Repo fetch check (CtrlHangul + Re-Bound)",
        "schedule": "0 9 * * *",
    },
    {
        "slug": "github-agent-ready-intake",
        "profile": "default",
        "id": "bf431b2a6ba6",
        "name": "GitHub agent-ready Issue intake",
        "schedule": "*/5 * * * *",
    },
    {
        "slug": "h4v3-broadcast-health",
        "profile": "dj-broadcast",
        "id": "27f6725028ff",
        "name": "H4V3 Broadcast Health Monitor",
        "schedule": "*/5 * * * *",
    },
)

# The existing intake script is authoritative for filtering, idempotency,
# Kanban projection, and reconciliation. GitHub events merely wake that same
# existing script earlier; they do not implement new policy in n8n.
GITHUB_REPOSITORIES: tuple[dict[str, str], ...] = (
    {"slug": "ctrl-hangul", "owner": "rhgo1749", "repository": "ctrl-hangul"},
    {"slug": "re-bound", "owner": "rhgo1749", "repository": "re-bound"},
    {"slug": "h4v3-dj", "owner": "rhgo1749", "repository": "H4V3-DJ"},
    {
        "slug": "h4v3-meowcore-avatar-lab",
        "owner": "rhgo1749",
        "repository": "h4v3-meowcore-avatar-lab",
    },
    {
        "slug": "h4v3-meowcore-voice-lab",
        "owner": "rhgo1749",
        "repository": "h4v3-meowcore-voice-lab",
    },
)
INTAKE_JOB = next(job for job in ACTIVE_JOBS if job["id"] == "bf431b2a6ba6")


def _uuid(key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, key))


def _http_node(name: str, action: str, job: dict[str, str], position: list[int]) -> dict[str, Any]:
    assert action in {"trigger", "pause"}
    url = (
        f"{PLACEHOLDER_URL}/api/cron/jobs/{job['id']}/{action}"
        f"?profile={job['profile']}"
    )
    return {
        "parameters": {
            "method": "POST",
            "url": url,
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "options": {
                # A trigger can have an unknown outcome after a timeout: it
                # may have reached Hermes even if n8n saw no response. Keep
                # the call short enough for the compensating pause to follow
                # within one existing 60-second ticker cycle.
                "timeout": TRIGGER_TIMEOUT_MS if action == "trigger" else PAUSE_TIMEOUT_MS,
                "response": {
                    "response": {
                        # An ambiguous trigger result must still reach cleanup.
                        # A failed pause remains an n8n execution failure.
                        "neverError": action == "trigger",
                        "responseFormat": "json",
                        "fullResponse": True,
                    }
                },
            },
        },
        "id": _uuid(f"{job['id']}:{action}"),
        "name": name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": position,
    }


def _wait_node(job: dict[str, str]) -> dict[str, Any]:
    return {
        "parameters": {"resume": "timeInterval", "amount": WAIT_SECONDS, "unit": "seconds"},
        "id": _uuid(f"{job['id']}:wait"),
        "name": "Wait for Hermes ticker",
        "type": "n8n-nodes-base.wait",
        "typeVersion": 1.1,
        "position": [640, 300],
    }


def _connections(first: str, trigger: str, pause: str) -> dict[str, Any]:
    return {
        first: {"main": [[{"node": trigger, "type": "main", "index": 0}]]},
        trigger: {"main": [[{"node": "Wait for Hermes ticker", "type": "main", "index": 0}]]},
        "Wait for Hermes ticker": {"main": [[{"node": pause, "type": "main", "index": 0}]]},
    }


def _base_workflow(name: str, nodes: list[dict[str, Any]], connections: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "nodes": nodes,
        "pinData": {},
        "connections": connections,
        "active": False,
        "settings": {"executionOrder": "v1"},
        "meta": {"templateCredsSetupCompleted": False},
        "tags": [],
    }


def schedule_workflow(job: dict[str, str]) -> dict[str, Any]:
    schedule_name = "Schedule Trigger"
    trigger_name = "Trigger existing Hermes cron job"
    pause_name = "Pause legacy Hermes schedule"
    schedule = {
        "parameters": {
            "rule": {
                "interval": [
                    {"field": "cronExpression", "expression": job["schedule"]}
                ]
            }
        },
        "id": _uuid(f"{job['id']}:schedule"),
        "name": schedule_name,
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [180, 300],
    }
    return _base_workflow(
        f"Hermes schedule · {job['name']}",
        [
            schedule,
            _http_node(trigger_name, "trigger", job, [400, 300]),
            _wait_node(job),
            _http_node(pause_name, "pause", job, [880, 300]),
        ],
        _connections(schedule_name, trigger_name, pause_name),
    )


def github_event_workflow(repo: dict[str, str]) -> dict[str, Any]:
    trigger_name = "GitHub Trigger"
    fire_name = "Trigger existing Hermes intake"
    pause_name = "Pause legacy Hermes intake schedule"
    github_trigger = {
        "parameters": {
            "authentication": "accessToken",
            "owner": {"__rl": True, "value": repo["owner"], "mode": "name"},
            "repository": {"__rl": True, "value": repo["repository"], "mode": "name"},
            "events": ["issues", "issue_comment", "pull_request"],
            "options": {"insecureSSL": False},
        },
        "id": _uuid(f"github:{repo['owner']}/{repo['repository']}:trigger"),
        "name": trigger_name,
        "type": "n8n-nodes-base.githubTrigger",
        "typeVersion": 1,
        "position": [180, 300],
    }
    return _base_workflow(
        f"Hermes GitHub event · {repo['owner']}/{repo['repository']} → intake",
        [
            github_trigger,
            _http_node(fire_name, "trigger", INTAKE_JOB, [400, 300]),
            _wait_node(INTAKE_JOB),
            _http_node(pause_name, "pause", INTAKE_JOB, [880, 300]),
        ],
        _connections(trigger_name, fire_name, pause_name),
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def write_templates(directory: Path) -> list[Path]:
    written: list[Path] = []
    for job in ACTIVE_JOBS:
        path = directory / f"schedule-{job['slug']}.json"
        _write_json(path, schedule_workflow(job))
        written.append(path)
    for repo in GITHUB_REPOSITORIES:
        path = directory / f"github-{repo['slug']}-intake.json"
        _write_json(path, github_event_workflow(repo))
        written.append(path)
    return written


def _is_private_http_target(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address in _TAILSCALE_CGNAT


def normalize_dashboard_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("dashboard URL must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("dashboard URL must not contain credentials")
    if not parsed.hostname:
        raise ValueError("dashboard URL must contain a host")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("dashboard URL must contain a valid port") from exc
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("dashboard URL must not contain a path, query, or fragment")
    if parsed.scheme == "http" and not _is_private_http_target(parsed.hostname):
        raise ValueError("http dashboard URL must use a loopback, private, or Tailnet IP address")
    return value


def render_templates(template_dir: Path, output_dir: Path, dashboard_url: str) -> list[Path]:
    normalized = normalize_dashboard_url(dashboard_url)
    templates = sorted(template_dir.glob("*.json"))
    if not templates:
        raise ValueError(f"no workflow templates found in {template_dir}")
    rendered: list[Path] = []
    for template in templates:
        raw = template.read_text(encoding="utf-8")
        if PLACEHOLDER_URL not in raw:
            raise ValueError(f"template is missing dashboard placeholder: {template}")
        data = json.loads(raw.replace(PLACEHOLDER_URL, normalized))
        serialized = json.dumps(data, ensure_ascii=False)
        if PLACEHOLDER_URL in serialized:
            raise ValueError(f"unresolved placeholder in {template}")
        path = output_dir / template.name
        _write_json(path, data)
        rendered.append(path)
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write-templates", action="store_true", help="regenerate tracked workflow templates")
    mode.add_argument("--dashboard-url", help="render importable workflows for this Hermes dashboard URL")
    parser.add_argument("--template-dir", type=Path, default=WORKFLOWS)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.write_templates:
            output = args.output_dir or WORKFLOWS
            paths = write_templates(output)
        else:
            output = args.output_dir or (ROOT / "state" / "rendered-workflows")
            paths = render_templates(args.template_dir, output, args.dashboard_url)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"workflow-render: ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"count": len(paths), "output_dir": str(output), "files": [p.name for p in paths]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
