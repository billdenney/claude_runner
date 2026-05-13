# Changelog

Notable changes to `claude-task-runner`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is pre-1.0 — minor versions can introduce breaking changes.
Breaking changes are called out in the version notes.

## [Unreleased]

### Added

- **Time-of-day-modulated 5h throttle bands** (ADR-0015). New section
  `[throttle.time_of_day]` defines core daytime / nighttime boundaries
  and a smooth ramp. `[throttle.five_hour]` gains four optional override
  fields (`{daytime,nighttime}_band_{full_dispatch,slowdown}_max_pct`)
  that the supervisor blends linearly across the ramp at each boundary.
  Default split (15/30 daytime, 50/75 nighttime) targets ~95% weekly
  cap consumption over 168h while leaving daytime headroom for the
  operator's interactive work. Backward-compatible: leave the override
  fields unset and the static bands from ADR-0004 apply unchanged.
- **Dynamic weekly pacing curve** (ADR-0016). When
  `[throttle.weekly].pacing_curve_enabled` (default `true`), the
  supervisor shifts the effective weekly bands based on observed-vs-target
  utilization at the current elapsed-in-week fraction, anchored to the
  OAuth-reported `seven_day.resets_at` (NOT a fixed weekday). The
  hard `pause_at_pct` floor is never modulated.
- **Nighttime-biased EOW push** (ADR-0015). New flag
  `[throttle.weekly].eow_push_nighttime_only` (default `true`) gates
  the `PAUSED_WEEKLY → END_OF_WEEK_PUSH` transition to core nighttime
  per `[throttle.time_of_day]`. EOW window widened 12h → 24h to give
  the gate more opportunities to fire.
- Two new pure modules under `supervisor/`: `time_of_day.py` and
  `pacing.py`. Both at 100% line + branch coverage.
- New `tests/integration/test_drift_canary.py` — exercises both the
  stream-json parser (via the bundled fake `claude` shim) and the
  `/usage` parser (via the `.cap` fixture corpus). Replaces the
  empty CI `drift-canary` job target.
- New `docs/cheatsheet.md` — quick-reference for tuning the throttle
  layers, with per-setting bump/lower guidance and cross-links to the
  ADRs.
- New ADRs: 0015 (time-of-day modulation) and 0016 (dynamic weekly
  pacing curve). ADR-0004 and ADR-0006 marked as amended.

### Changed

- `[throttle.weekly].eow_target_pct` lowered from 98 → 95. Leaves a 5pp
  safety margin against a burst-driven hard pause near reset.
- `[throttle.weekly].eow_window_s` widened from 43_200 (12h) → 86_400
  (24h). Pairs with the nighttime-biased EOW push gate.
- CI workflow `.github/workflows/ci.yml` adds `--cov-fail-under=75` to
  the test job. Previously no coverage gate was enforced. Aspirational
  90% target documented in the cheatsheet.
- `ThrottleSettings.five_hour` type changed from `ThrottleBandSettings`
  to a new `ThrottleFiveHourSettings` subclass. Test helpers updated
  in-place. Per-queue TOMLs that only set the existing `band_*` fields
  load without change.

### Fixed

- `runner.orchestrator._reap_finished` now uses `contextlib.suppress`
  instead of `try`/`except`/`pass` (ruff SIM105). Unblocked CI on every
  open PR.
- `runner.orchestrator._target_concurrency` long ternary reflowed to
  satisfy `ruff format --check` (hidden behind the SIM105 failure on
  pre-fix branches).

### Architecture / docs

- `docs/architecture.md` gains a "Per-tick band modulation" subsection
  describing the `_compute_effective_bands` pipeline and the points
  where `time_of_day` and `pacing` plug in.
- `docs/decisions/README.md` indexes ADRs 0015 and 0016.
