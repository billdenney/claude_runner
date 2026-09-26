"""Write sidecar request files the way a dispatched agent does.

The runner never writes a request: the ``agent-stop-and-ask`` skill has the
agent write ``request-NNN.json`` itself. Tests that need an open sidecar
write the same file with :func:`write_request`.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_task_runner.queue.schema import SidecarRequest
from claude_task_runner.queue.sidecar import request_path


def write_request(queue_dir: Path, request: SidecarRequest) -> Path:
    """Write ``request`` to its ``request-NNN.json`` path and return the path."""
    path = request_path(queue_dir, request.task_id, request.sequence)
    path.write_text(json.dumps(request.model_dump(mode="json"), indent=2), encoding="utf-8")
    return path
