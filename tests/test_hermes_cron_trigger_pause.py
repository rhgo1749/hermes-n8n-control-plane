#!/usr/bin/env python3
"""Preserve the historical Hermes cron trigger/pause migration fixture.

Current GitHub intake uses the direct actuator and does not require this cron
primitive. This temporary-HERMES_HOME test remains only to prove the legacy
cutover/rollback boundary can still interpret its historical one-job shape.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hermes-n8n-cron-flow-") as raw:
        home = Path(raw)
        scripts = home / "scripts"
        scripts.mkdir(mode=0o700)
        marker = home / "marker.log"
        script = scripts / "marker.sh"
        script.write_text(
            "#!/bin/sh\nset -eu\nprintf 'ran\\n' >> \"$HERMES_HOME/marker.log\"\nprintf 'ok\\n'\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        os.environ["HERMES_HOME"] = str(home)

        # Imports occur after HERMES_HOME is isolated because cron's storage
        # constants are process-local and derived from the home at import time.
        from cron.jobs import create_job, get_job, pause_job, trigger_job
        from cron.scheduler import tick

        job = create_job(
            None,
            "0 0 * * *",
            name="n8n route primitive canary",
            script="marker.sh",
            no_agent=True,
            deliver="local",
        )
        job_id = job["id"]
        initially_paused = pause_job(job_id)
        assert initially_paused is not None
        assert initially_paused["state"] == "paused"
        triggered = trigger_job(job_id)
        assert triggered is not None
        assert triggered["enabled"] is True
        assert tick(verbose=False, sync=True) == 1
        final = pause_job(job_id)
        assert final and final["enabled"] is False and final["state"] == "paused"
        persisted = get_job(job_id)
        assert persisted and persisted["last_status"] == "ok"
        assert marker.read_text(encoding="utf-8").splitlines() == ["ran"]

    print(json.dumps({"ok": True, "path": "trigger->tick->pause", "last_status": "ok"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
