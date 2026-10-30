"""Streams for fake workers, as ``Popen`` hands them to the dispatcher."""

from __future__ import annotations

import os
from typing import IO


def empty_stdout() -> IO[str]:
    """A worker's stdout after it printed nothing and exited: the read end
    of a pipe whose write end is closed. The dispatcher reads stdout through
    its descriptor, so a fake worker needs a real one. Close it when done."""
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    return open(read_fd, encoding="utf-8", errors="replace")
