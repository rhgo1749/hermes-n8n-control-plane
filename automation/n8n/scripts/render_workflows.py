#!/usr/bin/env python3
"""Generate the tracked n8n polling-fallback workflow template.

Repository-specific GitHub event workflows are intentionally absent. GitHub
event ingress is registry-driven through the loopback github-router.
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

ROUTER_FALLBACK_URL = "http://127.0.0.1:5681/fallback"
FALLBACK_TIMEOUT_MS = 120_000

_NAMESPACE = uuid.UUID("4d3669cd-39ce-4c84-a10d-762278d838c6")
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

ACTIVE_JOBS: tuple[dict[str, str], ...] = (
    {
        "slug": "github-agent-ready-intake",
        "profile": "default",
        "id": "bf431b2a6ba6",
        "name": "GitHub agent-ready Issue intake",
        "schedule": "*/5 * * * *",
    },
)


def _uuid(key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, key))


def _base_workflow(
    name: str,
    nodes: list[dict[str, Any]],
    connections: dict[str, Any],
) -> dict[str, Any]:
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
    fallback_name = "Run registry fallback"

    schedule = {
        "parameters": {
            "rule": {
                "interval": [
                    {
                        "field": "cronExpression",
                        "expression": job["schedule"],
                    }
                ]
            }
        },
        "id": _uuid(f"{job['id']}:schedule"),
        "name": schedule_name,
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [180, 300],
    }

    fallback = {
        "parameters": {
            "method": "POST",
            "url": ROUTER_FALLBACK_URL,
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "options": {
                "timeout": FALLBACK_TIMEOUT_MS,
                "response": {
                    "response": {
                        "neverError": False,
                        "responseFormat": "json",
                        "fullResponse": True,
                    }
                },
            },
        },
        "id": _uuid(f"{job['id']}:registry-fallback"),
        "name": fallback_name,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [440, 300],
    }

    return _base_workflow(
        f"Hermes schedule · {job['name']}",
        [schedule, fallback],
        {
            schedule_name: {
                "main": [
                    [
                        {
                            "node": fallback_name,
                            "type": "main",
                            "index": 0,
                        }
                    ]
                ]
            }
        },
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )


def write_templates(directory: Path) -> list[Path]:
    written: list[Path] = []
    for job in ACTIVE_JOBS:
        path = directory / f"schedule-{job['slug']}.json"
        _write_json(path, schedule_workflow(job))
        written.append(path)
    return written


def _is_private_http_target(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_loopback
        or address.is_private
        or address in _TAILSCALE_CGNAT
    )


def normalize_dashboard_url(raw: str) -> str:
    """Validate the legacy renderer URL argument without weakening old policy."""
    value = raw.strip().rstrip("/")
    parsed = urlparse(value)

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
    ):
        raise ValueError(
            "dashboard URL must be an absolute http(s) URL"
        )

    if parsed.username or parsed.password:
        raise ValueError(
            "dashboard URL must not contain credentials"
        )

    if not parsed.hostname:
        raise ValueError(
            "dashboard URL must contain a host"
        )

    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(
            "dashboard URL must contain a valid port"
        ) from exc

    if (
        parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "dashboard URL must not contain a path, query, or fragment"
        )

    if (
        parsed.scheme == "http"
        and not _is_private_http_target(parsed.hostname)
    ):
        raise ValueError(
            "http dashboard URL must use a loopback, private, or Tailnet IP address"
        )

    return value


def render_templates(
    template_dir: Path,
    output_dir: Path,
    dashboard_url: str,
) -> list[Path]:
    # Compatibility surface only. The current workflow is loopback-routed and
    # no longer substitutes this URL, but the old strict URL policy remains.
    normalize_dashboard_url(dashboard_url)

    templates = sorted(template_dir.glob("*.json"))
    if not templates:
        raise ValueError(
            f"no workflow templates found in {template_dir}"
        )

    rendered: list[Path] = []
    for template in templates:
        data = json.loads(template.read_text(encoding="utf-8"))
        path = output_dir / template.name
        _write_json(path, data)
        rendered.append(path)
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write-templates",
        action="store_true",
        help="regenerate tracked workflow templates",
    )
    mode.add_argument(
        "--dashboard-url",
        help="render importable workflows",
    )
    parser.add_argument(
        "--template-dir",
        type=Path,
        default=WORKFLOWS,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
    )
    args = parser.parse_args(argv)

    try:
        if args.write_templates:
            output = args.output_dir or WORKFLOWS
            paths = write_templates(output)
        else:
            output = (
                args.output_dir
                or ROOT / "state" / "rendered-workflows"
            )
            paths = render_templates(
                args.template_dir,
                output,
                args.dashboard_url,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"workflow-render: ERROR: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "count": len(paths),
                "output_dir": str(output),
                "files": [path.name for path in paths],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
