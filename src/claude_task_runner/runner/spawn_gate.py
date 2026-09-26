"""Keep a stopping supervisor from abandoning a worker it is starting.

A dispatch thread starts a ``claude`` worker with ``subprocess.Popen`` and
then records the worker's pid and log path in the task's state YAML. With
``[supervisor].adopt_workers`` on (ADR-0025) dispatch threads are daemon
threads, and a stopping supervisor exits without joining them. An exit
between those two steps leaves a running worker with no pid on record:
the next supervisor can neither adopt it nor see it, demotes the task and
dispatches it again, and two workers run the same task.

A :class:`SpawnGate` covers that window. :func:`runner.dispatcher.dispatch`
calls :meth:`SpawnGate.enter` just before it opens the attempt's log files
and starts the worker, and :meth:`SpawnGate.leave` once the pid is
recorded. :func:`supervisor.daemon.start_daemon` makes one gate per run
and calls :meth:`SpawnGate.close` before it releases the supervisor lock.
That waits, for at most a bound, for every thread inside the window, and
a daemon thread that reaches the gate afterwards starts nothing.
"""

from __future__ import annotations

import math
import threading


class SpawnGate:
    """Counts the dispatch threads that are starting a worker, per task."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._starting: dict[str, int] = {}
        self._closed = False

    def enter(self, task_id: str) -> None:
        """Record that this thread is starting ``task_id``'s worker.

        After :meth:`close`, a daemon thread waits here until the process
        exits, so it never starts its worker. Its task stays ``running``
        with no pid, which the next supervisor demotes and dispatches
        again, and nothing runs twice. A non-daemon thread
        (``adopt_workers`` off) goes on: the interpreter joins it at exit,
        and would wait forever for one parked here.
        """
        with self._cond:
            if self._closed and threading.current_thread().daemon:
                while True:
                    self._cond.wait()
            self._starting[task_id] = self._starting.get(task_id, 0) + 1

    def leave(self, task_id: str) -> None:
        """Record that ``task_id``'s worker has its pid on record, or that
        starting it failed.

        Raises :class:`ValueError` for a task that no thread entered with.
        """
        with self._cond:
            count = self._starting.get(task_id, 0)
            if count == 0:
                raise ValueError(f"no dispatch thread is starting a worker for task {task_id!r}")
            if count == 1:
                del self._starting[task_id]
            else:
                self._starting[task_id] = count - 1
            self._cond.notify_all()

    def close(self, timeout_s: float) -> list[str]:
        """Admit no more daemon threads, and wait up to ``timeout_s`` for
        the threads already inside.

        Returns the ids of the tasks whose workers were still being
        started when the wait ended, sorted: empty when every worker
        started so far has its pid on record. Raises :class:`ValueError`
        for a ``timeout_s`` that is negative, NaN or infinite.
        """
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError(
                f"timeout_s must be a finite number of seconds >= 0, got {timeout_s!r}"
            )
        with self._cond:
            self._closed = True
            self._cond.wait_for(lambda: not self._starting, timeout=timeout_s)
            return sorted(self._starting)
