#!/usr/bin/env python3
"""Generate the tracked n8n Webhook workflow for GitHub edge reconciliation.

The external GitHub webhook remains owned by ``github-router``.  n8n receives
only a bounded, authenticated loopback hop and is responsible for filtering
the two PR lifecycle events that may invoke the fixed edge-sync actuator.
"""
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"
EDGE_SYNC_WEBHOOK_PATH = "hermes-github-edge-sync"
EDGE_SYNC_ACTUATOR_URL = "http://127.0.0.1:5682/v1/edge-sync"
EDGE_SYNC_TIMEOUT_MS = 120_000
_NAMESPACE = uuid.UUID("4d3669cd-39ce-4c84-a10d-762278d838c6")
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

EDGE_SYNC_WORKFLOWS: tuple[dict[str, str], ...] = (
    {
        "slug": "github-pr-edge-sync",
        "name": "GitHub PR edge sync",
    },
)

def _uuid(key: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, key))


EDGE_SYNC_WORKFLOW_ID = _uuid("github-pr-edge-sync:workflow")
EDGE_SYNC_WORKFLOW_NAME = "Hermes Webhook · GitHub PR edge sync"
EDGE_SYNC_WEBHOOK_ID = _uuid("github-pr-edge-sync:webhook-id")
EDGE_SYNC_CREDENTIAL_ID = _uuid("github-pr-edge-sync:control-token-credential")
EDGE_SYNC_CREDENTIAL_NAME = "Hermes edge-sync control token"
EDGE_SYNC_CREDENTIAL_NODE_NAMES = (
    "GitHub edge sync webhook",
    "Run edge sync actuator",
)


def edge_sync_workflow(workflow: dict[str, str]) -> dict[str, Any]:
    webhook_name = "GitHub edge sync webhook"
    normalize_name = "Normalize bounded event"
    gate_name = "Allowed edge event?"
    sync_name = "Run edge sync actuator"
    ignore_name = "Ignore unsupported event"
    return {
        "id": EDGE_SYNC_WORKFLOW_ID,
        "name": EDGE_SYNC_WORKFLOW_NAME,
        "nodes": [
            {
                "parameters": {
                    "httpMethod": "POST",
                    "path": EDGE_SYNC_WEBHOOK_PATH,
                    "authentication": "headerAuth",
                    "responseMode": "lastNode",
                    "options": {},
                },
                "id": _uuid(f"{workflow['slug']}:webhook"),
                "webhookId": EDGE_SYNC_WEBHOOK_ID,
                "name": webhook_name,
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2.1,
                "position": [180, 300],
                "credentials": {
                    "httpHeaderAuth": {
                        "id": "REPLACE_AFTER_IMPORT",
                        "name": EDGE_SYNC_CREDENTIAL_NAME,
                    }
                },
            },
            {
                "parameters": {
                    "assignments": {
                        "assignments": [
                            {
                                "id": _uuid(f"{workflow['slug']}:repository"),
                                "name": "repository",
                                "value": "={{ $json.body.repository }}",
                                "type": "string",
                            },
                            {
                                "id": _uuid(f"{workflow['slug']}:event"),
                                "name": "event",
                                "value": "={{ $json.body.event }}",
                                "type": "string",
                            },
                            {
                                "id": _uuid(f"{workflow['slug']}:action"),
                                "name": "action",
                                "value": "={{ $json.body.action }}",
                                "type": "string",
                            },
                            {
                                "id": _uuid(f"{workflow['slug']}:merged"),
                                "name": "merged",
                                "value": "={{ $json.body.merged }}",
                                "type": "boolean",
                            },
                            {
                                "id": _uuid(f"{workflow['slug']}:label"),
                                "name": "label",
                                "value": "={{ $json.body.label }}",
                                "type": "string",
                            },
                            {
                                "id": _uuid(f"{workflow['slug']}:delivery"),
                                "name": "delivery",
                                "value": "={{ $json.body.delivery }}",
                                "type": "string",
                            },
                        ]
                    },
                    "includeOtherFields": False,
                    "options": {},
                },
                "id": _uuid(f"{workflow['slug']}:normalize"),
                "name": normalize_name,
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [420, 300],
            },
            {
                "parameters": {
                    "conditions": {
                        "options": {
                            "caseSensitive": True,
                            "leftValue": "",
                            "typeValidation": "strict",
                            "version": 2.3,
                        },
                        "conditions": [
                            {
                                "id": _uuid(f"{workflow['slug']}:allow"),
                                "leftValue": "={{ $json.event === 'pull_request' && (($json.action === 'closed' && $json.merged === true) || ($json.action === 'labeled' && $json.label === 'agent-rework')) }}",
                                "rightValue": True,
                                "operator": {
                                    "type": "boolean",
                                    "operation": "equals",
                                },
                            }
                        ],
                        "combinator": "and",
                    },
                },
                "id": _uuid(f"{workflow['slug']}:gate"),
                "name": gate_name,
                "type": "n8n-nodes-base.if",
                "typeVersion": 2.3,
                "position": [660, 300],
            },
            {
                "parameters": {
                    "method": "POST",
                    "url": EDGE_SYNC_ACTUATOR_URL,
                    "authentication": "genericCredentialType",
                    "genericAuthType": "httpHeaderAuth",
                    "sendBody": True,
                    "specifyBody": "json",
                    "jsonBody": "={{ JSON.stringify($json) }}",
                    "options": {"timeout": EDGE_SYNC_TIMEOUT_MS},
                },
                "id": _uuid(f"{workflow['slug']}:actuator"),
                "name": sync_name,
                "type": "n8n-nodes-base.httpRequest",
                "typeVersion": 4.4,
                "position": [920, 220],
                "credentials": {
                    "httpHeaderAuth": {
                        "id": "REPLACE_AFTER_IMPORT",
                        "name": EDGE_SYNC_CREDENTIAL_NAME,
                    }
                },
            },
            {
                "parameters": {
                    "assignments": {
                        "assignments": [
                            {
                                "id": _uuid(f"{workflow['slug']}:ignored"),
                                "name": "ok",
                                "value": "true",
                                "type": "boolean",
                            },
                            {
                                "id": _uuid(f"{workflow['slug']}:ignored-flag"),
                                "name": "ignored",
                                "value": "true",
                                "type": "boolean",
                            },
                            {
                                "id": _uuid(f"{workflow['slug']}:ignored-reason"),
                                "name": "reason",
                                "value": "unsupported_pull_request_action",
                                "type": "string",
                            },
                        ]
                    },
                    "includeOtherFields": False,
                    "options": {},
                },
                "id": _uuid(f"{workflow['slug']}:ignore"),
                "name": ignore_name,
                "type": "n8n-nodes-base.set",
                "typeVersion": 3.4,
                "position": [920, 380],
            },
        ],
        "pinData": {},
        "connections": {
            webhook_name: {
                "main": [[{"node": normalize_name, "type": "main", "index": 0}]]
            },
            normalize_name: {
                "main": [[{"node": gate_name, "type": "main", "index": 0}]]
            },
            gate_name: {
                "main": [
                    [{"node": sync_name, "type": "main", "index": 0}],
                    [{"node": ignore_name, "type": "main", "index": 0}],
                ]
            },
        },
        "active": False,
        "settings": {"executionOrder": "v1"},
        "meta": {"templateCredsSetupCompleted": False},
        "tags": [],
    }


def build_runtime_credential(
    token: str,
    *,
    credential_id: str = EDGE_SYNC_CREDENTIAL_ID,
    credential_name: str = EDGE_SYNC_CREDENTIAL_NAME,
) -> list[dict[str, Any]]:
    """Build the one decrypted credential record used only during host import.

    n8n's server CLI encrypts plain ``data`` on import.  Keeping this helper
    pure makes it possible to test the credential envelope without ever
    storing a real control token in the repository.
    """
    if not isinstance(token, str):
        raise ValueError("credential token must be text")
    token = token.strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("credential token must be a single non-empty value")
    if not isinstance(credential_id, str) or not credential_id.strip():
        raise ValueError("credential id must be non-empty")
    if not isinstance(credential_name, str) or not credential_name.strip():
        raise ValueError("credential name must be non-empty")
    return [
        {
            "id": credential_id,
            "name": credential_name,
            "type": "httpHeaderAuth",
            "data": {
                "name": "Authorization",
                "value": f"Bearer {token}",
            },
        }
    ]


def bind_runtime_credential(
    workflow: dict[str, Any],
    *,
    credential_id: str = EDGE_SYNC_CREDENTIAL_ID,
    credential_name: str = EDGE_SYNC_CREDENTIAL_NAME,
    workflow_id: str = EDGE_SYNC_WORKFLOW_ID,
) -> dict[str, Any]:
    """Return an inactive workflow with one credential bound to both HTTP nodes."""
    if not isinstance(credential_id, str) or not credential_id.strip():
        raise ValueError("credential id must be non-empty")
    if not isinstance(credential_name, str) or not credential_name.strip():
        raise ValueError("credential name must be non-empty")
    if (
        not isinstance(workflow_id, str)
        or not workflow_id
        or workflow_id != workflow_id.strip()
        or any(character.isspace() for character in workflow_id)
    ):
        raise ValueError("workflow id must be one non-empty value")

    runtime = copy.deepcopy(workflow)
    runtime["id"] = workflow_id
    runtime["active"] = False
    runtime["meta"] = {
        **(runtime.get("meta") or {}),
        "templateCredsSetupCompleted": True,
    }

    raw_nodes = runtime.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError("workflow nodes must be a list")
    nodes = {
        node.get("name"): node
        for node in raw_nodes
        if isinstance(node, dict)
    }
    expected_types = {
        "GitHub edge sync webhook": "n8n-nodes-base.webhook",
        "Run edge sync actuator": "n8n-nodes-base.httpRequest",
    }
    for node_name in EDGE_SYNC_CREDENTIAL_NODE_NAMES:
        node = nodes.get(node_name)
        if not isinstance(node, dict):
            raise ValueError(f"required credential node is missing: {node_name}")
        if node.get("type") != expected_types[node_name]:
            raise ValueError(f"unexpected credential node type: {node_name}")
        node["credentials"] = {
            "httpHeaderAuth": {
                "id": credential_id,
                "name": credential_name,
            }
        }
    return runtime


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def write_templates(directory: Path) -> list[Path]:
    written: list[Path] = []
    for workflow in EDGE_SYNC_WORKFLOWS:
        path = directory / f"{workflow['slug']}.json"
        _write_json(path, edge_sync_workflow(workflow))
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
        raise ValueError(
            "http dashboard URL must use a loopback, private, or Tailnet IP address"
        )
    return value


def render_templates(
    template_dir: Path,
    output_dir: Path,
    dashboard_url: str | None = None,
) -> list[Path]:
    if dashboard_url:
        # Retain the old private-dashboard validation for callers that still
        # pass the compatibility option; no rendered workflow uses it.
        normalize_dashboard_url(dashboard_url)
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []
    for template in sorted(template_dir.glob("*.json")):
        if template.name.startswith("schedule-"):
            raise ValueError(
                "scheduled n8n workflows are retired; use the Webhook template"
            )
        data = json.loads(template.read_text(encoding="utf-8"))
        path = output_dir / template.name
        _write_json(path, data)
        rendered.append(path)
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write-templates", action="store_true")
    parser.add_argument(
        "--dashboard-url",
        help="Deprecated compatibility argument; no workflow uses this URL",
    )
    parser.add_argument("--template-dir", type=Path, default=WORKFLOWS)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.write_templates:
            output = args.output_dir or WORKFLOWS
            paths = write_templates(output)
        else:
            output = args.output_dir or ROOT / "state" / "rendered-workflows"
            paths = render_templates(args.template_dir, output, args.dashboard_url)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"workflow-render: ERROR: {exc}", file=sys.stderr)
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
