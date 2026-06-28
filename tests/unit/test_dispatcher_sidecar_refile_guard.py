"""Sidecar re-file loop guard arithmetic + schema wiring (ADR-0027)."""

from claude_task_runner.config.schema import FailureClassifierSettings
from claude_task_runner.queue.schema import TaskState
from claude_task_runner.runner.dispatcher import _sidecar_refile_decision


def test_no_progress_increments_below_threshold():
    assert _sidecar_refile_decision(0, made_progress=False, threshold=4) == (1, False)
    assert _sidecar_refile_decision(2, made_progress=False, threshold=4) == (3, False)


def test_no_progress_trips_at_threshold():
    assert _sidecar_refile_decision(3, made_progress=False, threshold=4) == (4, True)
    # past the threshold stays tripped
    assert _sidecar_refile_decision(7, made_progress=False, threshold=4) == (8, True)


def test_progress_resets_counter_and_never_trips():
    assert _sidecar_refile_decision(3, made_progress=True, threshold=4) == (0, False)
    assert _sidecar_refile_decision(99, made_progress=True, threshold=4) == (0, False)


def test_threshold_one_trips_on_first_refile():
    assert _sidecar_refile_decision(0, made_progress=False, threshold=1) == (1, True)


def test_taskstate_sidecar_refile_count_defaults_zero():
    ts = TaskState(task_id="t1")
    assert ts.sidecar_refile_count == 0


def test_failure_classifier_threshold_default_is_four():
    s = FailureClassifierSettings(
        environmental_patterns=[],
        operator_patterns=[],
        task_patterns=[],
        failure_circuit_breaker_threshold=3,
    )
    assert s.sidecar_refile_loop_threshold == 4
