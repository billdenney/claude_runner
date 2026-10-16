"""Launch the file-backed fake worker, ``tests/fixtures/claude_shim/file_worker.py``.

The shim models a ``claude --print`` worker that outlived the supervisor
that spawned it: its stdout is its stream log, and it runs in a session of
its own, re-parented to init.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

WORKER = Path(__file__).parent.parent / "fixtures" / "claude_shim" / "file_worker.py"


def spawn_file_worker(log_path: Path, env_extra: dict[str, str]) -> int:
    """Launch the file-backed worker; return the (init-owned) worker pid.

    The launcher forks the real worker, prints its pid, and exits, so the
    worker is re-parented to init (no zombie for this test process) and
    its pid genuinely disappears on exit — which is what the adopter's
    liveness probe needs. The log exists when this returns.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **env_extra}
    out = subprocess.run(
        [sys.executable, str(WORKER), str(log_path)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return int(out.stdout.strip())
