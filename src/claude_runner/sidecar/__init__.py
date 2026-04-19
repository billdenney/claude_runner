"""Sidecar-file based operator interaction (stop-and-ask protocol).

A task that needs operator input writes a JSON ``request-<seq>.json`` file to
the per-task sidecar directory and then exits ``end_turn``. The runner
notices the open request, moves the task to
:class:`claude_runner.models.TaskStatus.AWAITING_INPUT`, and reports it
through ``events.ndjson`` and ``.claude_runner/status_snapshot.json``. The
operator supplies answers via ``claude-runner input <task_id>``; the runner
detects the matching ``response-<seq>.json``, flips the task to
:class:`claude_runner.models.TaskStatus.READY_TO_RESUME`, and dispatches it
again (via ``--resume <session_id>``) with the answers prepended to the new
user prompt.
"""

from claude_runner.sidecar.schema import (
    Answer,
    Interaction,
    InteractionRequest,
    InteractionResponse,
    Option,
    Question,
    RequestState,
)
from claude_runner.sidecar.store import (
    SidecarStore,
    SidecarValidationError,
)

__all__ = [
    "Answer",
    "Interaction",
    "InteractionRequest",
    "InteractionResponse",
    "Option",
    "Question",
    "RequestState",
    "SidecarStore",
    "SidecarValidationError",
]
