#!/usr/bin/env python3
"""Compatibility renderer for tracked n8n workflow templates.

The GitHub intake is event-driven through ``github-router`` and no longer owns
an n8n Schedule Trigger workflow.  The renderer remains so older host tooling
can safely report an empty template set without recreating the retired five-
minute polling workflow.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"

# Intentionally empty.  The durable Hermes cron job still exists and stays
# paused between event-driven trigger/pause leases; n8n does not schedule it.
ACTIVE_JOBS: tuple[dict[str, str], ...] = ()

_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def write_templates(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    return []


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

    if (
        parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "dashboard URL must not contain a path, query, or fragment"
        )

    if parsed.scheme == "http" and not _is_private_http_target(parsed.hostname):
        raise ValueError(
            "http dashboard URL must use a loopback, private, or Tailnet IP address"
        )

    return value


def render_templates(
    template_dir: Path,
    output_dir: Path,
    dashboard_url: str,
) -> list[Path]:
    # Compatibility surface only.  Current GitHub intake routing is handled by
    # github-router and has no n8n workflow template to render.
    normalize_dashboard_url(dashboard_url)
    output_dir.mkdir(parents=True, exist_ok=True)

    templates = sorted(template_dir.glob("*.json"))
    rendered: list[Path] = []
    for template in templates:
        data = json.loads(template.read_text(encoding="utf-8"))
        path = output_dir / template.name
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        rendered.append(path)
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write-templates",
        action="store_true",
        help="report the current tracked workflow template set",
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
            output = args.output_dir or ROOT / "state" / "rendered-workflows"
            paths = render_templates(
                args.template_dir,
                output,
                args.dashboard_url,
            )
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
