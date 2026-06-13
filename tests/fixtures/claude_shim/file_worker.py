#!/usr/bin/env python3
"""Fake file-backed `claude` worker for ADR-0025 adoption e2e tests.

Unlike the stdout-streaming ``claude`` shim, this one writes its
stream-json directly to a FILE — modelling a worker whose stdout the
dispatcher redirected to ``attempt-N.stream.jsonl``.

It **forks** so the process that actually writes the log is re-parented
to init (PID 1) once this launcher exits, exactly like a real ``claude``
worker that outlived the supervisor that spawned it. This matters for the
adoption test: the adopter probes liveness with ``os.kill(pid, 0)``, and
a worker that is a *child* of the still-running test process would linger
as a zombie after exiting (``os.kill`` on a zombie succeeds), so the
adopter would never see it die. Re-parenting to init means init reaps it
on exit, so the pid genuinely disappears.

The launcher (which the test's ``Popen`` owns) prints the worker (child)
pid on stdout and exits immediately, so the test can record the pid to
monitor and reap its own short-lived launcher without blocking.

Usage::

    file_worker.py <log_path>     # prints "<worker_pid>\\n", exits 0

Env vars:

* ``WORKER_SESSION_ID`` (default ``adopt-e2e``)
* ``WORKER_STOP_REASON`` (default ``end_turn``)
* ``WORKER_IS_ERROR``    (default ``false``)
* ``WORKER_PRESLEEP_S``  (default ``0``) — grandchild sleeps this long
  BEFORE writing anything (lets the adopter attach while it is still
  "live" and quiet)
* ``WORKER_NO_RESULT``   (default ``""``) — if set, the grandchild writes
  init + assistant but NO terminal result event, then exits (models a
  crash mid-run)
"""

from __future__ import annotations

import json
import os
import sys
import time


def _write_stream(log_path: str) -> int:
    session_id = os.environ.get("WORKER_SESSION_ID", "adopt-e2e")
    stop_reason = os.environ.get("WORKER_STOP_REASON", "end_turn")
    is_error = os.environ.get("WORKER_IS_ERROR", "false").lower() == "true"
    presleep = float(os.environ.get("WORKER_PRESLEEP_S", "0"))
    no_result = bool(os.environ.get("WORKER_NO_RESULT", ""))

    if presleep:
        time.sleep(presleep)

    with open(log_path, "a", encoding="utf-8") as fh:

        def emit(obj: dict) -> None:
            fh.write(json.dumps(obj) + "\n")
            fh.flush()

        emit({"type": "system", "subtype": "init", "session_id": session_id})
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "working"}],
                    "usage": {"input_tokens": 30, "output_tokens": 20},
                },
            }
        )
        if no_result:
            return 1
        emit(
            {
                "type": "result",
                "subtype": "error" if is_error else "success",
                "stop_reason": stop_reason,
                "is_error": is_error,
                "total_cost_usd": 0.03,
                "duration_ms": 100,
                "usage": {"input_tokens": 30, "output_tokens": 20},
            }
        )
    return 1 if is_error else 0


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: file_worker.py <log_path>\n")
        return 2
    log_path = sys.argv[1]

    # Fork: the child becomes the file-backed worker; the launcher (this
    # process, owned by the test's Popen) prints the worker pid and exits
    # immediately so the worker is re-parented to init. The test then
    # reaps the short-lived launcher (no zombie) and monitors the worker
    # pid — which, owned by init, genuinely disappears on exit.
    worker_pid = os.fork()
    if worker_pid > 0:
        sys.stdout.write(f"{worker_pid}\n")
        sys.stdout.flush()
        return 0

    # Worker (child): detach into its own session (mirrors the
    # dispatcher's start_new_session). Crucially, redirect stdin/stdout/
    # stderr to /dev/null so the worker does NOT keep the launcher's
    # captured stdout pipe open — otherwise ``subprocess.run(
    # capture_output=True)`` in the test would block reading that pipe
    # until the worker (which inherited the fd) exits, defeating the
    # "launch and return while the worker keeps running" contract. Use
    # os._exit so the forked child doesn't run pytest/atexit machinery.
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os._exit(_write_stream(log_path))


if __name__ == "__main__":
    sys.exit(main())
