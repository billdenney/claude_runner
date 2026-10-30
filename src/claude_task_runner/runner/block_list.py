"""The dispatch block-list (ADR-0029): which task ids it blocks.

``[dispatch].dispatch_block_file`` names a JSONL file in the queue. The
selector skips every task it blocks, and the terminal-close gate (ADR-0033)
checks it before it adds a row, so both read it through
:func:`blocked_task_ids`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from claude_task_runner.once import first_time

logger = logging.getLogger(__name__)


def blocked_task_ids(queue_dir: Path, block_file: str | None) -> set[str]:
    """The task ids ``block_file`` blocks: those of its rows that are JSON
    objects with ``block_dispatch`` true and a non-empty string ``task``.

    ``block_file`` is relative to ``queue_dir``; ``None`` or empty means the
    feature is off, and blocks nothing. Each line is parsed on its own. A
    row that is JSON but no block, such as an index row or a ``target_path``
    re-acquisition row, contributes nothing, as does a blank line, so a
    mixed block-list (the ``needs_acquisition.jsonl`` convention) is read
    without acting on rows that are not blocks. A line that is not JSON
    contributes nothing either, with a WARNING naming the file and line,
    once per process for each line's text: it may be a damaged block row,
    and its task then dispatches.

    The file is read as UTF-8, a byte-order mark allowed, with a byte that
    is not UTF-8 read as U+FFFD. A row whose bad byte breaks its JSON is
    such a line. One whose bad byte is inside a string still parses: in a
    note it still blocks, and in the ``task`` id it blocks no real task.

    A file that is missing or cannot be read blocks nothing, with a DEBUG
    line: the task dispatches and its pre-dispatch hook re-checks the
    block, which wastes a defer at worst, where a block-list that blocked
    too much would strand a runnable task.
    """
    if not block_file:
        return set()
    path = queue_dir / block_file
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        # ValueError: a NUL byte in the configured path, which no file has.
        logger.debug("dispatch block-list %s unreadable (%s); treating as empty", path, exc)
        return set()

    blocked: set[str] = set()
    for number, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError as exc:
            if first_time(("block_list_line", str(path), number, stripped)):
                logger.warning(
                    "dispatch block-list %s line %d is not JSON (%s); it blocks nothing",
                    path,
                    number,
                    exc,
                )
            continue
        if isinstance(row, dict) and row.get("block_dispatch") is True:
            task = row.get("task")
            if isinstance(task, str) and task:
                blocked.add(task)
    return blocked
