#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "automation" / "n8n" / "scripts" / "install-intake-actuator.sh"


def _text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_installer_shell_syntax() -> None:
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_systemd_boundary_pins_canonical_source_path() -> None:
    text = _text()
    assert "--env HERMES_AGENT_SOURCE_ROOT=$HERMES_AGENT_SOURCE_ROOT" in text
    assert "--env PYTHONPATH=$HERMES_AGENT_SOURCE_ROOT" in text
    assert 'export PYTHONPATH="$HERMES_AGENT_SOURCE_ROOT"' in text


def test_unit_stops_exact_in_container_actuator() -> None:
    text = _text()
    exact = (
        "^/opt/venv/bin/python3[[:space:]]+"
        "/home/hermes/.local/libexec/github_intake_actuator.py$"
    )
    exec_stop = next(
        line for line in text.splitlines() if line.startswith("ExecStop=")
    )
    assert "pkill -TERM -f" in exec_stop
    assert exact in exec_stop


def test_upgrade_reaps_pre_execstop_orphan_before_start() -> None:
    text = _text()
    stop_at = text.index('systemctl stop "$UNIT_NAME"')
    reap_at = text.index('pids="$(pgrep -f', stop_at)
    start_at = text.index('systemctl start "$UNIT_NAME"')
    assert stop_at < reap_at < start_at
    assert "kill -TERM $pids" in text[reap_at:start_at]
    assert "kill -KILL $pids" in text[reap_at:start_at]


def test_post_start_reads_back_single_pid_pythonpath() -> None:
    text = _text()
    start_at = text.index('systemctl start "$UNIT_NAME"')
    post_start = text[start_at:]
    assert 'count="$(printf "%s\\n" "$pids" | sed "/^$/d" | wc -l)"' in post_start
    assert '[ "$count" -eq 1 ]' in post_start
    assert 'tr "\\000" "\\n" < "/proc/$pid/environ"' in post_start
    assert '[ "$actual" = "$expected" ]' in post_start
    assert 'fail "actuator process identity/PYTHONPATH did not converge"' in post_start


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
