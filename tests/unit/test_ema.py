"""Tests for runner.ema — per-task-type EMA + JSON persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import EMAPrior, EMASettings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.ema import (
    EMA_FILE_NAME,
    EMAFile,
    EMAFileError,
    TaskTypeEMA,
    list_buckets,
    load,
    predict_duration_s,
    predict_tokens,
    task_type_key,
    update_bucket,
    write_atomic,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 5, 3, 18, 0, tzinfo=UTC))


def _settings(*, alpha: float = 0.3, warmup: int = 3) -> EMASettings:
    return EMASettings(
        alpha=alpha,
        prior_warmup_samples=warmup,
        runtime_p90_multiplier=1.5,
        priors={
            "claude-opus-4-7": {
                "high": EMAPrior(tokens=3_000_000, duration_s=1800),
                "max": EMAPrior(tokens=6_000_000, duration_s=3600),
            },
        },
    )


def _task(
    *,
    model: str = "claude-opus-4-7",
    effort: str = "high",
    tools: list[str] | None = None,
    tags: list[str] | None = None,
) -> Task:
    return Task(
        id="t",
        title="t",
        prompt="p",
        model=model,
        effort=effort,
        allowed_tools=tools or [],
        tags=tags or [],
    )


class TestTaskTypeKey:
    def test_stable_across_tool_order(self) -> None:
        a = _task(tools=["Read", "Write", "Bash"])
        b = _task(tools=["Bash", "Write", "Read"])
        assert task_type_key(a) == task_type_key(b)

    def test_differs_by_model(self) -> None:
        a = _task(model="claude-opus-4-7")
        b = _task(model="claude-sonnet-4-6")
        assert task_type_key(a) != task_type_key(b)

    def test_differs_by_effort(self) -> None:
        assert task_type_key(_task(effort="high")) != task_type_key(_task(effort="max"))

    def test_cohort_tag_overrides(self) -> None:
        base = _task()
        custom = _task(tags=["ema-cohort:papers-fast"])
        assert task_type_key(base) != task_type_key(custom)


class TestUpdateBucket:
    def test_first_sample_seeds_directly(self, clock: FakeClock) -> None:
        ema = EMAFile()
        out = update_bucket(
            ema,
            "k",
            observed_tokens=1_000_000,
            observed_duration_s=600,
            clock=clock,
            alpha=0.3,
        )
        bucket = out.buckets["k"]
        assert bucket.sample_count == 1
        assert bucket.token_ema == 1_000_000.0
        assert bucket.duration_s_ema == 600.0
        assert bucket.last_updated == clock.now()

    def test_subsequent_smooths(self, clock: FakeClock) -> None:
        ema = EMAFile()
        ema = update_bucket(
            ema,
            "k",
            observed_tokens=1_000_000,
            observed_duration_s=600,
            clock=clock,
            alpha=0.5,
        )
        clock.advance(60)
        ema = update_bucket(
            ema,
            "k",
            observed_tokens=2_000_000,
            observed_duration_s=1200,
            clock=clock,
            alpha=0.5,
        )
        bucket = ema.buckets["k"]
        assert bucket.sample_count == 2
        assert bucket.token_ema == 0.5 * 2_000_000 + 0.5 * 1_000_000
        assert bucket.duration_s_ema == 0.5 * 1200 + 0.5 * 600

    def test_pure_does_not_mutate_input(self, clock: FakeClock) -> None:
        original = EMAFile()
        update_bucket(
            original,
            "k",
            observed_tokens=100,
            observed_duration_s=10,
            clock=clock,
            alpha=0.3,
        )
        assert original.buckets == {}

    def test_invalid_alpha_rejected(self, clock: FakeClock) -> None:
        ema = EMAFile()
        with pytest.raises(ValueError, match="alpha"):
            update_bucket(
                ema, "k", observed_tokens=1, observed_duration_s=1, clock=clock, alpha=0.0
            )
        with pytest.raises(ValueError, match="alpha"):
            update_bucket(
                ema, "k", observed_tokens=1, observed_duration_s=1, clock=clock, alpha=1.5
            )

    def test_negative_observation_rejected(self, clock: FakeClock) -> None:
        ema = EMAFile()
        with pytest.raises(ValueError):
            update_bucket(
                ema, "k", observed_tokens=-1, observed_duration_s=10, clock=clock, alpha=0.3
            )
        with pytest.raises(ValueError):
            update_bucket(
                ema, "k", observed_tokens=1, observed_duration_s=-1, clock=clock, alpha=0.3
            )


class TestPredictTokens:
    def test_no_bucket_no_prior_returns_zero(self) -> None:
        ema = EMAFile()
        task = _task(model="claude-newmodel-99", effort="medium")
        assert predict_tokens(ema, task, settings=_settings()) == 0.0

    def test_no_bucket_uses_prior(self) -> None:
        ema = EMAFile()
        task = _task(model="claude-opus-4-7", effort="high")
        assert predict_tokens(ema, task, settings=_settings()) == 3_000_000.0

    def test_warm_bucket_replaces_prior(self, clock: FakeClock) -> None:
        task = _task()
        ema = EMAFile()
        # Get to warmup_samples=3 quickly
        for _ in range(3):
            ema = update_bucket(
                ema,
                task_type_key(task),
                observed_tokens=500_000,
                observed_duration_s=300,
                clock=clock,
                alpha=0.5,
            )
        # EMA value is ~500_000 (all observations equal); should override prior
        assert predict_tokens(ema, task, settings=_settings()) == pytest.approx(500_000, rel=1e-6)

    def test_partial_warmup_blends(self, clock: FakeClock) -> None:
        task = _task()
        ema = EMAFile()
        # 1 sample of 1M tokens, with prior of 3M and warmup=3
        ema = update_bucket(
            ema,
            task_type_key(task),
            observed_tokens=1_000_000,
            observed_duration_s=300,
            clock=clock,
            alpha=0.5,
        )
        # blend_weight = 1/3, so 1/3 * 1M + 2/3 * 3M = 333k + 2M = ~2.33M
        result = predict_tokens(ema, task, settings=_settings(warmup=3))
        assert result == pytest.approx(1_000_000 / 3 + 3_000_000 * 2 / 3, rel=1e-6)


class TestPredictDuration:
    def test_uses_prior(self) -> None:
        task = _task()
        ema = EMAFile()
        assert predict_duration_s(ema, task, settings=_settings()) == 1800.0


class TestPersistence:
    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        ema = load(tmp_path / EMA_FILE_NAME)
        assert ema.buckets == {}

    def test_round_trip(self, tmp_path: Path, clock: FakeClock) -> None:
        ema = EMAFile()
        ema = update_bucket(
            ema,
            "k",
            observed_tokens=1_000_000,
            observed_duration_s=600,
            clock=clock,
            alpha=0.3,
        )
        path = tmp_path / EMA_FILE_NAME
        write_atomic(ema, path)
        loaded = load(path)
        assert loaded == ema

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / EMA_FILE_NAME
        path.write_text("{not json")
        with pytest.raises(EMAFileError, match="invalid JSON"):
            load(path)

    def test_unknown_schema_version_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / EMA_FILE_NAME
        path.write_text('{"schema_version": 99, "buckets": {}}')
        with pytest.raises(EMAFileError, match="schema_version=99"):
            load(path)


class TestListBuckets:
    def test_sorted(self, clock: FakeClock) -> None:
        ema = EMAFile(
            buckets={
                "k_b": TaskTypeEMA(task_type="k_b", sample_count=1),
                "k_a": TaskTypeEMA(task_type="k_a", sample_count=1),
            }
        )
        keys = [b.task_type for b in list_buckets(ema)]
        assert keys == ["k_a", "k_b"]
