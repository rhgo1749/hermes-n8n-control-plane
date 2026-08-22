"""Shared bounded edge-sync runtime configuration contract.

This module is copied beside both the loopback actuator and the standalone
completion observer at installation time. Keep the parser and numeric contract
in one source so both wake paths reject the same unsafe configuration before
spawning the edge process.
"""
from __future__ import annotations

import math
import os

EDGE_SYNC_TIMEOUT_ENV = "HERMES_EDGE_SYNC_TIMEOUT_SECONDS"
DEFAULT_EDGE_SYNC_TIMEOUT_SECONDS = 120.0
MAX_EDGE_SYNC_TIMEOUT_SECONDS = 3600.0
EDGE_SYNC_TIMEOUT_GRACE_SECONDS = 5.0
EDGE_SYNC_OUTPUT_LIMIT_BYTES = 64 * 1024


class TimeoutConfigurationError(ValueError):
    """Stable, secret-free invalid timeout configuration error."""

    code = "edge_timeout_invalid"


def parse_edge_sync_timeout(raw: str | None = None) -> float:
    """Return a finite positive timeout within the shared maximum."""
    value = os.environ.get(EDGE_SYNC_TIMEOUT_ENV, "") if raw is None else raw
    if not isinstance(value, str):
        raise TimeoutConfigurationError
    value = value.strip()
    if not value:
        return DEFAULT_EDGE_SYNC_TIMEOUT_SECONDS
    try:
        configured = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TimeoutConfigurationError from exc
    if (
        not math.isfinite(configured)
        or configured <= 0
        or configured > MAX_EDGE_SYNC_TIMEOUT_SECONDS
    ):
        raise TimeoutConfigurationError
    return configured
