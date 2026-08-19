#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
N8N = ROOT / "automation" / "n8n"
sys.path.insert(0, str(N8N / "scripts"))
from render_workflows import ACTIVE_JOBS, normalize_dashboard_url  # noqa: E402


def validate_dashboard_url_policy() -> None:
    assert normalize_dashboard_url("https://n8n.example.com") == "https://n8n.example.com"
    assert normalize_dashboard_url("http://100.107.12.90:9119") == "http://100.107.12.90:9119"
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
        raise AssertionError(f"accepted unsafe dashboard URL: {value}")


def validate_single_intake_boundary() -> None:
    expected_jobs = {"bf431b2a6ba6": "default"}
    plugin_source = (
        ROOT / "hermes-plugin" / "n8n-cron-auth" / "__init__.py"
    ).read_text(encoding="utf-8")
    plugin_tree = ast.parse(plugin_source)
    allowed_jobs = None
    for node in ast.walk(plugin_tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "ALLOWED_JOBS"
        ):
            assert node.value is not None
            allowed_jobs = ast.literal_eval(node.value)
            break
    assert allowed_jobs == expected_jobs
    installer_source = (
        N8N / "scripts" / "configure-hermes-service-auth.sh"
    ).read_text(encoding="utf-8")
    installer_match = re.search(
        r"(?ms)^expected = (?P<mapping>\{.*?^\})",
        installer_source,
    )
    assert installer_match is not None
    assert ast.literal_eval(installer_match.group("mapping")) == expected_jobs
    assert "create_job" not in plugin_source
    assert "delete" not in plugin_source


def main() -> int:
    # Async-only intake owns no tracked n8n Schedule/GitHub Trigger workflows.
    # The durable Hermes job remains allowlisted below and is woken by the
    # loopback github-router -> lease-controller path.
    assert ACTIVE_JOBS == ()
    assert sorted((N8N / "workflows").glob("schedule-*.json")) == []
    assert sorted((N8N / "workflows").glob("github-*-intake.json")) == []

    validate_dashboard_url_policy()
    validate_single_intake_boundary()

    compose_text = (N8N / "compose.yaml").read_text(encoding="utf-8")
    compose = yaml.safe_load(compose_text)
    n8n = compose["services"]["n8n"]
    assert n8n["restart"] == "unless-stopped"
    assert n8n["network_mode"] == "host"
    assert "ports" not in n8n
    assert n8n["environment"]["N8N_LISTEN_ADDRESS"] == "127.0.0.1"
    assert n8n["environment"]["N8N_CONCURRENCY_PRODUCTION_LIMIT"] == "1"
    assert n8n["environment"]["N8N_BLOCK_ENV_ACCESS_IN_NODE"] == "true"
    assert n8n["environment"]["N8N_PUBLIC_API_DISABLED"] == "true"

    lease = compose["services"]["lease-controller"]
    assert lease["restart"] == "unless-stopped"
    assert lease["network_mode"] == "host"
    assert lease["read_only"] is True
    assert lease["user"] == "1000:1000"
    assert lease["environment"]["LEASE_LISTEN_PORT"] == "5680"
    assert lease["environment"]["LEASE_HERMES_JOB_ID"] == "bf431b2a6ba6"
    assert lease["environment"]["LEASE_HERMES_PROFILE"] == "default"
    assert (
        lease["environment"]["LEASE_STATE_PATH"]
        == "/state/hermes-intake-lease.json"
    )

    assert n8n["environment"]["N8N_DIAGNOSTICS_ENABLED"] == "false"
    assert n8n["environment"]["N8N_PERSONALIZATION_ENABLED"] == "false"
    assert n8n["environment"]["N8N_TEMPLATES_ENABLED"] == "false"

    router = compose["services"]["github-router"]
    assert router["restart"] == "unless-stopped"
    assert router["network_mode"] == "host"
    assert router["read_only"] is True
    assert router["user"] == "1000:1000"
    assert router["environment"]["GITHUB_ROUTER_LISTEN_PORT"] == "5681"
    assert (
        router["environment"]["GITHUB_ROUTER_LEASE_BASE_URL"]
        == "http://127.0.0.1:5680"
    )
    assert router["environment"]["GITHUB_ROUTER_STATE_PATH"] == "/state/github-router.json"
    router_volumes = "\n".join(router["volumes"])
    assert "./state/secrets:/run/secrets:ro" in router_volumes
    assert "/var/run/docker.sock" not in compose_text

    router_source = (N8N / "github-router" / "router.py").read_text(encoding="utf-8")
    assert 'LISTEN_HOST = "127.0.0.1"' in router_source
    assert "X-Hub-Signature-256" in router_source
    assert "hmac.compare_digest" in router_source
    assert '"/github/hermes-intake"' in router_source
    assert '"/scope/claim"' in router_source
    assert '"/fallback"' in router_source
    assert '"/reconcile"' in router_source
    assert "_enqueue_scope" in router_source
    assert "_claim_scope" in router_source
    post_start = router_source.index("    def do_POST(self) -> None:")
    auth_index = router_source.index(
        'authorization = self.headers.get("Authorization", "").strip()',
        post_start,
    )
    claim_index = router_source.index(
        'if parsed.path == "/scope/claim":',
        post_start,
    )
    assert auth_index < claim_index

    intake_source = (
        ROOT
        / "automation"
        / "hermes"
        / "scripts"
        / "github-agent-ready-kanban-intake.py"
    ).read_text(encoding="utf-8")
    assert "HERMES_INTAKE_SCOPE_TOKEN_FILE" in intake_source
    assert 'headers={"Authorization": f"Bearer {token}"}' in intake_source

    env_example = (N8N / ".env.example").read_text(encoding="utf-8")
    assert "N8N_PORT=5678" in env_example
    assert "N8N_HOST_PORT" not in env_example
    assert "LEASE_HERMES_BASE_URL=" in env_example
    assert "GITHUB_ROUTER_OWNER=" in env_example
    assert "GITHUB_ROUTER_TOPIC=" in env_example
    assert "GITHUB_ROUTER_PUBLIC_URL=" in env_example

    print(
        json.dumps(
            {
                "ok": True,
                "schedule_workflows": 0,
                "github_workflows": 0,
                "github_event_router": 1,
                "hermes_intake_job": "default:bf431b2a6ba6",
                "hermes_schedule_owned_by_n8n": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
