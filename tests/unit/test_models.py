"""Tests for the usage data models (Pydantic)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from claude_task_runner.usage.models import UsageReading, WindowReading


def _w(pct: int) -> WindowReading:
    return WindowReading(utilization_pct=pct, resets_at_raw="2am (UTC)", resets_at=None)


class TestWindowReading:
    def test_valid(self) -> None:
        w = _w(50)
        assert w.utilization_pct == 50
        assert w.resets_at_raw == "2am (UTC)"
        assert w.resets_at is None

    def test_negative_pct_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WindowReading(utilization_pct=-1, resets_at_raw="2am (UTC)", resets_at=None)

    def test_over_100_pct_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WindowReading(utilization_pct=101, resets_at_raw="2am (UTC)", resets_at=None)

    def test_frozen(self) -> None:
        w = _w(50)
        with pytest.raises(ValidationError):
            w.utilization_pct = 99  # type: ignore[misc]


class TestUsageReading:
    def test_valid(self) -> None:
        r = UsageReading(
            captured_at=datetime(2026, 5, 3, 18, 0, tzinfo=UTC),
            five_hour=_w(50),
            seven_day=_w(20),
        )
        assert r.schema_version == 2
        assert r.five_hour.utilization_pct == 50
        assert r.capture_path is None

    def test_capture_path_round_trip(self) -> None:
        r = UsageReading(
            captured_at=datetime(2026, 5, 3, tzinfo=UTC),
            five_hour=_w(50),
            seven_day=_w(20),
            capture_path="/tmp/foo.cap",
        )
        copy = r.model_copy(update={"capture_path": "/tmp/bar.cap"})
        assert copy.capture_path == "/tmp/bar.cap"
        # Original is frozen and unchanged
        assert r.capture_path == "/tmp/foo.cap"
