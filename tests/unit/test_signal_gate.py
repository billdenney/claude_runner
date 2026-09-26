"""Known answers for tests/signal_gate.py, the gate every test runs under.

The class tests hand the gate recording fakes in place of the real
``os.kill`` and ``os.killpg``, so a gate that failed would still send nothing.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from signal_gate import SignalGate, SignalGateError

TESTS_DIR = Path(__file__).resolve().parents[1]


class Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def kill(self, pid: int, sig: int) -> None:
        self.calls.append(("kill", pid, sig))

    def killpg(self, pgid: int, sig: int) -> None:
        self.calls.append(("killpg", pgid, sig))


@pytest.fixture
def sent() -> Sent:
    return Sent()


@pytest.fixture
def gate(sent: Sent) -> SignalGate:
    return SignalGate(sent.kill, sent.killpg)


@pytest.fixture
def stranger(signal_gate: SignalGate) -> Iterator[int]:
    """A live process in its own session that this test started but that
    no longer descends from it: the launcher exits and init adopts it."""
    out = subprocess.run(
        ["bash", "-c", "setsid sleep 600 >/dev/null 2>&1 < /dev/null & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(out.stdout)
    for _ in range(100):  # setsid execs sleep; wait for its new session
        if os.getpgid(pid) == pid:
            break
        time.sleep(0.01)
    yield pid
    os.kill(pid, 0)  # still running: the gate under test sent it nothing
    signal_gate.allow(pid)
    os.kill(pid, signal.SIGKILL)


def test_every_test_runs_under_the_gate(signal_gate: SignalGate) -> None:
    assert os.kill == signal_gate.kill
    assert os.killpg == signal_gate.killpg


@pytest.mark.parametrize(
    ("pid", "reason"),
    [
        (1, "init"),
        (-1, "every process the user owns"),
        (0, "a whole process group"),
        (-4242, "a whole process group"),
    ],
)
def test_blocks_a_pid_of_one_or_less(gate: SignalGate, sent: Sent, pid: int, reason: str) -> None:
    message = f"os.kill({pid}, SIGTERM) targets {reason}"
    with pytest.raises(SignalGateError) as exc_info:
        gate.kill(pid, signal.SIGTERM)
    assert str(exc_info.value) == message
    assert gate.violations == [message]
    assert sent.calls == []


def test_blocks_the_test_process_itself(gate: SignalGate, sent: Sent) -> None:
    with pytest.raises(SignalGateError) as exc_info:
        gate.kill(os.getpid(), signal.SIGHUP)
    assert str(exc_info.value) == (
        f"os.kill({os.getpid()}, SIGHUP) targets the test process itself "
        "(mark the test allow_self_signal if that is the point)"
    )
    assert sent.calls == []


def test_allow_self_lets_the_test_process_signal_itself(sent: Sent) -> None:
    SignalGate(sent.kill, sent.killpg, allow_self=True).kill(os.getpid(), signal.SIGHUP)
    assert sent.calls == [("kill", os.getpid(), signal.SIGHUP)]


def test_blocks_a_process_the_test_did_not_start(gate: SignalGate, sent: Sent) -> None:
    parent = os.getppid()
    with pytest.raises(SignalGateError) as exc_info:
        gate.kill(parent, signal.SIGCONT)
    assert (
        str(exc_info.value)
        == f"os.kill({parent}, SIGCONT) targets a process this test did not start"
    )
    assert sent.calls == []


@pytest.mark.parametrize(
    ("pgid", "reason"),
    [
        (1, "every process the user owns (killpg(1) is kill(-1))"),
        (0, "the test process's own group"),
        (-7, "a negative process group"),
    ],
)
def test_blocks_a_group_of_one_or_less(
    gate: SignalGate, sent: Sent, pgid: int, reason: str
) -> None:
    with pytest.raises(SignalGateError) as exc_info:
        gate.killpg(pgid, signal.SIGKILL)
    assert str(exc_info.value) == f"os.killpg({pgid}, SIGKILL) targets {reason}"
    assert sent.calls == []


def test_blocks_its_own_group(gate: SignalGate, sent: Sent) -> None:
    with pytest.raises(SignalGateError) as exc_info:
        gate.killpg(os.getpgrp(), signal.SIGTERM)
    assert str(exc_info.value) == (
        f"os.killpg({os.getpgrp()}, SIGTERM) targets the test process's own group"
    )
    assert sent.calls == []


def test_blocks_a_group_the_test_did_not_start(gate: SignalGate, sent: Sent, stranger: int) -> None:
    with pytest.raises(SignalGateError) as exc_info:
        gate.killpg(stranger, signal.SIGTERM)
    assert (
        str(exc_info.value)
        == f"os.killpg({stranger}, SIGTERM) targets a group this test did not start"
    )
    assert sent.calls == []


def test_allow_vouches_for_a_process_init_adopted(
    gate: SignalGate, sent: Sent, stranger: int
) -> None:
    gate.allow(stranger)
    gate.killpg(stranger, signal.SIGTERM)
    gate.kill(stranger, signal.SIGTERM)
    assert sent.calls == [("killpg", stranger, signal.SIGTERM), ("kill", stranger, signal.SIGTERM)]
    assert gate.violations == []


def test_lets_a_child_and_its_group_through(
    gate: SignalGate, sent: Sent, live_worker_pid: int
) -> None:
    gate.kill(live_worker_pid, signal.SIGTERM)
    gate.killpg(os.getpgid(live_worker_pid), signal.SIGKILL)
    assert sent.calls == [
        ("kill", live_worker_pid, signal.SIGTERM),
        ("killpg", live_worker_pid, signal.SIGKILL),
    ]
    assert gate.violations == []


def test_lets_probes_through(gate: SignalGate, sent: Sent) -> None:
    gate.kill(1, 0)
    gate.killpg(1, 0)
    assert sent.calls == [("kill", 1, 0), ("killpg", 1, 0)]


def test_blocks_a_scan_of_a_tree_the_test_did_not_start(gate: SignalGate) -> None:
    with pytest.raises(SignalGateError) as exc_info:
        gate.check_scan_root(1)
    assert str(exc_info.value) == (
        "the stuck-sleep-loop scan walked the real /proc from pid 1, which this test did "
        "not start; inject stuck_loop_detect_fn, terminate_fn and sigterm_fn, or set "
        "bash_poll_antipattern_kill=False"
    )


def test_lets_a_scan_of_a_child_through(gate: SignalGate, live_worker_pid: int) -> None:
    gate.check_scan_root(live_worker_pid)
    assert gate.violations == []


def test_fails_a_test_that_swallows_the_error(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error alone is not enough: code under test may catch it."""
    pytester.makepyfile(
        """
        import os
        import signal


        def test_swallows():
            try:
                os.kill(os.getppid(), signal.SIGCONT)
            except AssertionError:
                pass
        """
    )
    env_path = os.pathsep.join(filter(None, [str(TESTS_DIR), os.environ.get("PYTHONPATH")]))
    monkeypatch.setenv("PYTHONPATH", env_path)
    result = pytester.runpytest_subprocess("-p", "signal_gate", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["*signal gate: os.kill(*, SIGCONT) targets a process this test did not start*"]
    )
