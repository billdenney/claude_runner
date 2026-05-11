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


class ThrottleBandSettings(_StrictModel):
    budget_tokens: int = Field(gt=0)
    band_full_dispatch_max_pct: int = Field(ge=0, le=100)
    band_slowdown_max_pct: int = Field(ge=0, le=100)


class ThrottleWeeklySettings(ThrottleBandSettings):
    pause_at_pct: int = Field(ge=0, le=100)
    eow_push_enter_at_pct: int = Field(ge=0, le=100)
    eow_target_pct: int = Field(ge=0, le=100)
    eow_window_s: float = Field(ge=0)
    eow_runtime_safety_factor: float = Field(gt=0, le=1.0)


class ThrottleSettings(_StrictModel):
    five_hour: ThrottleBandSettings
    weekly: ThrottleWeeklySettings


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

    ``plan`` selects an entry from the top-level ``[plans.*]`` table so
    the loader can auto-tune ``[throttle.*]`` budgets and bands against
    the plan's 5h:weekly token ratio. Empty string means "no auto-tune;
    use the explicit [throttle.*] values as-is."
    """

    executable: str = "claude"
    config_dir: str = ""
    plan: str = ""


class PlanSettings(_StrictModel):
    """Per-plan token-budget and throttle-band hints.

    The loader uses these to derive ``[throttle.*]`` budgets and bands
    when ``[claude].plan`` is set. Operator overrides in
    ``claude_runner.toml`` still win on a per-field basis (the merged
    explicit fields take precedence over plan-derived defaults).
    """

    five_hour_tokens: int = Field(gt=0)
    weekly_tokens: int = Field(gt=0)
    band_full_dispatch_max_pct: int = Field(ge=0, le=100)
    band_slowdown_max_pct: int = Field(ge=0, le=100)


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
    plans: dict[str, PlanSettings] = Field(default_factory=dict)
