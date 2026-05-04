# ADR-0009: Inject Clock protocol everywhere (testability)

- **Date:** 2026-05-03
- **Status:** accepted

## Context

The supervisor's behavior depends critically on time: window-reset
detection, end-of-week push, drift recovery polling, watchdog backoff.
Tests of this logic should not need `time.sleep()` or `freezegun`.

## Decision

A `Clock` protocol with `now()` and (optionally) `monotonic()` methods
is injected into every module that needs time. `RealClock` calls
`datetime.now(timezone.utc)`; `FakeClock` advances on demand in tests.

```python
class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...

class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)
    def monotonic(self) -> float:
        return time.monotonic()

class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start
        self._mono = 0.0
    def now(self) -> datetime:
        return self._now
    def monotonic(self) -> float:
        return self._mono
    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds
```

Code uses `clock.now()` instead of `datetime.now()`. Direct calls to
`datetime.utcnow()`, `datetime.now()`, or `time.monotonic()` outside of
`clock.py` are flagged in code review.

## Alternatives considered

- **`freezegun` library:** works but slows tests; requires care with
  patching imports; harder to reason about than explicit injection.
- **Time-mocking via `mock.patch`:** brittle; tests break when imports
  move.

## Consequences

- (+) Pure-function state machine: `step(state, reading, clock) → ...`
  is trivially testable.
- (+) Window-boundary tests run in microseconds.
- (-) Slightly more boilerplate (passing `clock` around).

## Reversibility

Low — once the codebase is structured around dependency injection,
removing it would be a major refactor. But this is the desired direction.
