"""Pydantic settings model.

The full settings tree is built up incrementally as components land. Slices
are accessed via attribute (`settings.usage`, `settings.throttle.five_hour`,
etc.) so a component only depends on the section it reads.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Base for all settings sections — forbid unknown keys to catch typos."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class UsageSettings(_StrictModel):
    capture_trust_timeout_s: float = Field(gt=0)
    capture_usage_timeout_s: float = Field(gt=0)
    capture_eof_timeout_s: float = Field(gt=0)
    capture_post_ready_pad_ms: int = Field(ge=0)
    capture_post_data_pad_ms: int = Field(ge=0)
    capture_rotation_count: int = Field(ge=0)
    poll_interval_s: float = Field(gt=0)
    healthcheck_interval_s: float = Field(gt=0)
    suspicious_delta_pct: int = Field(ge=0, le=100)
    drift_recovery_clean_polls: int = Field(ge=1)


class TimeOfDaySettings(_StrictModel):
    """Global day/night boundaries used by per-band time-of-day overrides.

    The operator is interactive during the day (e.g. 06:00-22:00 local), so
    dispatch should be tighter then to leave token headroom; at night the
    runner can use the budget more aggressively. The transition is linearly
    interpolated over a ``ramp_minutes`` window around each boundary so
    dispatch doesn't whipsaw at the clock edge.

    See ADR-0015 for the rationale and the math.
    """

    timezone: str = ""
    """IANA timezone name (e.g. ``"America/New_York"``). Empty string means
    "use system local time" (``datetime.astimezone(None)``)."""

    day_start: str = "06:00"
    """Inclusive start of core daytime, ``HH:MM`` 24-hour."""

    day_end: str = "22:00"
    """Exclusive end of core daytime (i.e. start of evening ramp toward night)."""

    ramp_minutes: int = Field(default=30, ge=0, le=180)
    """Width of the linear day/night interpolation ramp around each boundary."""


class ThrottleBandSettings(_StrictModel):
    """Static band thresholds — always available as a fallback."""

    budget_tokens: int = Field(gt=0)
    band_full_dispatch_max_pct: int = Field(ge=0, le=100)
    band_slowdown_max_pct: int = Field(ge=0, le=100)


class ThrottleFiveHourSettings(ThrottleBandSettings):
    """5-hour bands with optional daytime/nighttime overrides.

    When any of the four override fields is non-None, the supervisor uses
    it in place of the corresponding ``band_*`` value per the time-of-day
    schedule defined in ``[throttle.time_of_day]``. Leaving them at ``None``
    falls back to the static bands (full backward compatibility).
    """

    daytime_band_full_dispatch_max_pct: int | None = Field(default=None, ge=0, le=100)
    daytime_band_slowdown_max_pct: int | None = Field(default=None, ge=0, le=100)
    nighttime_band_full_dispatch_max_pct: int | None = Field(default=None, ge=0, le=100)
    nighttime_band_slowdown_max_pct: int | None = Field(default=None, ge=0, le=100)


class ThrottleWeeklySettings(ThrottleBandSettings):
    """Weekly bands plus EOW push and dynamic pacing curve.

    The pacing curve (when ``pacing_curve_enabled``) shifts the effective
    ``band_*`` thresholds up or down based on how far the observed weekly
    utilization is from the target curve at the current point in the
    weekly window. See ADR-0016.
    """

    pause_at_pct: int = Field(ge=0, le=100)
    eow_push_enter_at_pct: int = Field(ge=0, le=100)
    eow_target_pct: int = Field(ge=0, le=100)
    eow_window_s: float = Field(ge=0)
    eow_runtime_safety_factor: float = Field(gt=0, le=1.0)

    pacing_curve_enabled: bool = False
    """Master switch for the dynamic weekly pacing curve."""

    pre_eow_target_pct: int = Field(default=80, ge=0, le=100)
    """Target utilization at the start of the EOW push window — the curve
    ramps from 0 to here over (1 - eow_window_fraction) of the week, then
    from here to ``eow_target_pct`` over the EOW window."""

    pacing_slack_pp: float = Field(default=10.0, ge=0, le=100)
    """Dead-band (percentage points) around the target curve. The bands
    only shift when observed deviates by more than ``slack`` from target."""

    eow_push_nighttime_only: bool = True
    """When ``True`` (the default), the PAUSED_WEEKLY → END_OF_WEEK_PUSH
    transition fires only during core nighttime per ``[throttle.time_of_day]``.
    This keeps daytime 5h windows available for interactive use while the
    end-of-week burn-down runs overnight. See ADR-0015."""


class ThrottleSettings(_StrictModel):
    five_hour: ThrottleFiveHourSettings
    weekly: ThrottleWeeklySettings
    time_of_day: TimeOfDaySettings = Field(default_factory=TimeOfDaySettings)


class ConcurrencySettings(_StrictModel):
    max_concurrency: int = Field(ge=1)
    initial_concurrency: int = Field(ge=1)


class EMAPrior(_StrictModel):
    tokens: int = Field(gt=0)
    duration_s: float = Field(gt=0)


class EMASettings(_StrictModel):
    alpha: float = Field(gt=0.0, le=1.0)
    prior_warmup_samples: int = Field(ge=0)
    runtime_p90_multiplier: float = Field(gt=0)
    priors: dict[str, dict[str, EMAPrior]] = Field(default_factory=dict)


class SessionSettings(_StrictModel):
    max_resume_attempts: int = Field(ge=0)
    resume_fail_fast_s: float = Field(gt=0)


class FailureClassifierSettings(_StrictModel):
    environmental_patterns: list[str]
    operator_patterns: list[str]
    task_patterns: list[str]
    failure_circuit_breaker_threshold: int = Field(ge=1)


class TaskCapsSettings(_StrictModel):
    max_tokens_per_task: int = Field(ge=0)
    max_duration_s_per_task: float = Field(ge=0)
    heartbeat_silence_alert_s: float = Field(gt=0)
    heartbeat_silence_kill_s: float = Field(ge=0)


class WatchdogSettings(_StrictModel):
    restart_cooldown_s: float = Field(gt=0)
    restart_backoff_max_s: float = Field(gt=0)
    crash_loop_threshold: int = Field(ge=1)


class SupervisorSettings(_StrictModel):
    window_start_delay_s: float = Field(ge=0)
    state_file: str
    sigterm_grace_s: float = Field(gt=0)
    dry_run: bool
    preferred_init_system: str  # auto | systemd | cron


class NotifySettings(_StrictModel):
    channels: list[str]
    desktop_command: str
    file_path: str
    webhook_url: str
    email_to: str


class HookSettings(_StrictModel):
    pre_dispatch_command: str
    pre_dispatch_timeout_s: float = Field(gt=0)
    post_dispatch_command: str
    post_dispatch_timeout_s: float = Field(gt=0)


class SidecarSettings(_StrictModel):
    unanswered_alert_s: float = Field(ge=0)
    unanswered_auto_recommended_s: float = Field(ge=0)


class MetricsSettings(_StrictModel):
    prometheus_enabled: bool
    prometheus_textfile_path: str


class UiSettings(_StrictModel):
    refresh_interval_ms: int = Field(gt=0)


class FixturesSettings(_StrictModel):
    rotation_window_days: int = Field(ge=0)


class ClaudeSettings(_StrictModel):
    """How to invoke the ``claude`` CLI.

    Most importantly, ``config_dir`` lets a single host operate against
    multiple Claude accounts (e.g. work + personal): each account has its
    own ``CLAUDE_CONFIG_DIR`` populated by ``claude /login`` once, and
    each per-queue ``claude_runner.toml`` points at the appropriate dir.
    Empty string means "use claude's default" (``~/.claude``).
    """

    executable: str = "claude"
    config_dir: str = ""


class Settings(_StrictModel):
    """Root settings model — the merged effective configuration."""

    claude: ClaudeSettings
    usage: UsageSettings
    throttle: ThrottleSettings
    concurrency: ConcurrencySettings
    ema: EMASettings
    effort_levels: dict[str, list[str]]
    session: SessionSettings
    failure_classifier: FailureClassifierSettings
    task_caps: TaskCapsSettings
    watchdog: WatchdogSettings
    supervisor: SupervisorSettings
    notify: NotifySettings
    hooks: HookSettings
    sidecar: SidecarSettings
    metrics: MetricsSettings
    ui: UiSettings
    fixtures: FixturesSettings
