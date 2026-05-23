"""Pydantic settings model.

The full settings tree is built up incrementally as components land. Slices
are accessed via attribute (`settings.usage`, `settings.throttle.five_hour`,
etc.) so a component only depends on the section it reads.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

UsageSourceMode = Literal["tty", "api", "api_then_tty"]
"""Which production :class:`UsageSource` the daemon should use.

* ``"tty"`` (default) — spawn ``claude /usage`` and parse the TUI.
  Slow (10-30s/capture) but reads exactly what the operator sees.
* ``"api"`` — read rate-limit headers from a minimal
  ``/v1/messages`` call. Fast and cheap (~4 tokens/poll) but the
  headers are reverse-engineered and the OAuth token can expire.
* ``"api_then_tty"`` — API path with TTY fall-through on documented
  API failures (auth-expired / missing-header / network). The TTY
  fall-through also refreshes the OAuth token as a side effect.
"""


class _StrictModel(BaseModel):
    """Base for all settings sections — forbid unknown keys to catch typos."""

    model_config = ConfigDict(extra="forbid", frozen=True)


_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
"""Account names must be filesystem- and CLI-safe.

Used as state-file keys, supervisor log fields, and `account list/pause`
arguments — restricting to alnum / underscore / hyphen / dot keeps
operator workflows obvious and shell-friendly.
"""


class UsageSettings(_StrictModel):
    source: UsageSourceMode = "tty"
    """Which :class:`UsageSource` implementation to use. See
    :data:`UsageSourceMode` for the trade-offs.

    The default ``"tty"`` keeps the historical behaviour. Operators
    who want the fast path opt in by setting ``[usage].source =
    "api_then_tty"`` (recommended over ``"api"`` because the TTY
    fall-through also refreshes the OAuth token on expiry)."""

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
    api_timeout_s: float = Field(default=10.0, gt=0)
    """Per-request timeout for the API usage source (seconds).
    Generous enough to swallow ordinary network jitter, tight enough
    that the daemon's tick loop doesn't stall on a stuck connection.
    Ignored when ``source = "tty"``."""

    api_probe_model: str = "claude-haiku-4-5"
    """Cheapest model for the API probe call. The choice only affects
    cost; the rate-limit headers come back the same regardless.
    Ignored when ``source = "tty"``."""


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


class LoggingSettings(_StrictModel):
    """Process-wide logging knobs honoured by :mod:`observability`.

    The supervisor's log lines flow through ``structlog`` processors
    and out to stderr (where systemd's user service captures them
    into the journal). The defaults are picked for ``journalctl
    --user -u claude-task-runner`` readability.
    """

    level: str = "INFO"
    """Stdlib log-level name (``"DEBUG"``, ``"INFO"``, ``"WARNING"``,
    ``"ERROR"``). The root logger is set to this level; anything
    below is dropped at the handler. INFO is the operator-facing
    default; DEBUG is useful when tracking a specific dispatch."""

    format: Literal["text", "json"] = "text"
    """Output format. ``"text"`` (default) renders as
    ``timestamp [level] logger key=value …`` — readable in
    ``journalctl``. ``"json"`` renders as one JSON object per line,
    suitable for shipping to Loki / Vector / etc."""


class ClaudeSettings(_StrictModel):
    """How to invoke the ``claude`` CLI.

    ``config_dir`` is a *legacy* single-account alias. New configurations
    should declare one or more ``[[accounts]]`` blocks at the top level
    instead. When ``[[accounts]]`` is absent, the loader synthesises a
    single account named ``"default"`` from this field for backwards
    compatibility. When both are present, ``[[accounts]]`` wins and this
    field is ignored. Empty string means "use claude's default"
    (``~/.claude``).

    ``plan`` selects an entry from the top-level ``[plans.*]`` table so
    the loader can auto-tune ``[throttle.*]`` budgets and bands against
    the plan's 5h:weekly token ratio. Empty string means "no auto-tune;
    use the explicit [throttle.*] values as-is."
    """

    executable: str = "claude"
    config_dir: str = ""
    plan: str = ""


class AccountSettings(_StrictModel):
    """Queue-side declaration of one Claude account to dispatch through.

    Multi-account dispatch lets a single supervisor drain the same
    ``todo/`` queue against multiple Claude billing identities in
    parallel — when one account hits the weekly cap, the others keep
    pulling. Each account has its own ``CLAUDE_CONFIG_DIR`` populated
    by ``claude /login`` once.

    The queue-side block lists *which* accounts to dispatch through;
    per-account dispatch policy (concurrency cap, throttle bands,
    time-of-day cutover) lives inside each account's own config dir
    at ``<config_dir>/runner-account.toml``. The loader reads that
    file separately and composes a :class:`ResolvedAccount`. This
    keeps each account owner in control of their own limits — useful
    when accounts are billed to different people / entities.

    All accounts are equal priority. The dispatcher picks the least-
    utilized account (by 5h utilization, tie-broken by weekly util
    then name) when multiple have free capacity. There is intentionally
    no queue-wide concurrency ceiling — operators choose per-account
    caps such that their sum is acceptable.

    ``linux_user`` (when set) opts the account into multi-user spawning:
    the supervisor invokes ``claude`` for this account via
    ``sudo -n -u <linux_user> env CLAUDE_CONFIG_DIR=... ...``. Useful
    when a second account is billed to a different person / entity and
    must run under a separate Linux uid for audit clarity. See the
    multi-Linux-user ADR for the operator setup (passwordless sudo).
    """

    name: str
    """Operator-visible account label; appears in supervisor logs, the
    state YAML's RunRecord.account field, and the ``account`` CLI. Must
    be filesystem- and shell-safe (alnum / underscore / hyphen / dot,
    not starting with hyphen or dot, max 64 chars)."""

    config_dir: str
    """``CLAUDE_CONFIG_DIR`` for this account. Empty string targets
    claude's default ``~/.claude``. The loader also reads per-account
    dispatch policy from ``<config_dir>/runner-account.toml``."""

    linux_user: str | None = None
    """Optional Linux uid name to spawn claude as. When set, the
    supervisor uses ``sudo -n -u <linux_user>`` to invoke claude for
    this account. ``None`` (the default) spawns as the supervisor's
    own user."""

    @model_validator(mode="after")
    def _validate_name(self) -> AccountSettings:
        if not _ACCOUNT_NAME_RE.match(self.name):
            raise ValueError(
                f"account name {self.name!r} must match {_ACCOUNT_NAME_RE.pattern!r} "
                "(alnum / underscore / hyphen / dot, max 64 chars, must not start "
                "with hyphen or dot)"
            )
        if self.linux_user is not None and not self.linux_user.strip():
            raise ValueError("linux_user must be a non-empty Linux username when set")
        return self


class AccountConcurrencyPolicy(_StrictModel):
    """Per-account concurrency cap from ``<config_dir>/runner-account.toml``.

    Default is intentionally low (1): the supervisor doesn't trust an
    untested account to handle parallel sessions until the operator
    has observed it under load and raised the cap.
    """

    max_concurrency: int = Field(default=1, ge=1)


class AccountThrottleFiveHour(_StrictModel):
    """Per-account 5-hour throttle band overrides.

    Every field is ``int | None``: ``None`` (the default) means
    *inherit* the queue-wide ``[throttle.five_hour]`` value for that
    field. Set a field to override the queue-wide value for this
    account only — useful when a fresh / untested account needs
    tighter bands than the established one, or vice versa.

    Changed in PR 13: pre-PR-13 these had hardcoded defaults
    (40/60/70/90) which were the *same* numbers the queue uses by
    default — so the visible behaviour didn't change. After PR 13
    these are None-by-default and a missing field falls through to
    the queue-wide value, which gives operators a single source of
    truth (the queue toml) and lets per-account files override
    surgically.
    """

    daytime_band_full_dispatch_max_pct: int | None = Field(default=None, ge=0, le=100)
    daytime_band_slowdown_max_pct: int | None = Field(default=None, ge=0, le=100)
    nighttime_band_full_dispatch_max_pct: int | None = Field(default=None, ge=0, le=100)
    nighttime_band_slowdown_max_pct: int | None = Field(default=None, ge=0, le=100)


class AccountThrottleWeekly(_StrictModel):
    """Per-account weekly throttle + pacing-curve overrides.

    Every field is ``T | None`` with ``None`` meaning "inherit the
    queue-wide ``[throttle.weekly]`` value for that field." Set a
    field to override.

    Use cases:
      * One account is the operator's primary; the other is a
        secondary that's allowed to push closer to its weekly cap
        (``pause_at_pct`` higher, ``pacing_slack_pp`` wider).
      * ``eow_push_nighttime_only`` is true queue-wide (default) so
        daytime windows stay free for interactive use, but is false
        on a fully-autonomous account that can push 24/7.

    Added in PR 13. The hard pause floor (``pause_at_pct``) is still
    a SAFETY floor even when overridden — the pacing curve never
    tightens past it.
    """

    band_full_dispatch_max_pct: int | None = Field(default=None, ge=0, le=100)
    band_slowdown_max_pct: int | None = Field(default=None, ge=0, le=100)
    pause_at_pct: int | None = Field(default=None, ge=0, le=100)
    eow_push_enter_at_pct: int | None = Field(default=None, ge=0, le=100)
    eow_target_pct: int | None = Field(default=None, ge=0, le=100)
    eow_window_s: float | None = Field(default=None, ge=0)
    eow_runtime_safety_factor: float | None = Field(default=None, gt=0, le=1.0)
    pacing_curve_enabled: bool | None = None
    pre_eow_target_pct: int | None = Field(default=None, ge=0, le=100)
    pacing_slack_pp: float | None = Field(default=None, ge=0, le=100)
    eow_push_nighttime_only: bool | None = None


class AccountTimeOfDay(_StrictModel):
    """Per-account day/night cutover override.

    ``day_end`` was hardcoded to "21:00" pre-PR-13; now defaults to
    None (inherit queue-wide ``[throttle.time_of_day].day_end``).
    """

    day_end: str | None = None


class AccountThrottlePolicy(_StrictModel):
    five_hour: AccountThrottleFiveHour = Field(default_factory=AccountThrottleFiveHour)
    weekly: AccountThrottleWeekly = Field(default_factory=AccountThrottleWeekly)
    time_of_day: AccountTimeOfDay = Field(default_factory=AccountTimeOfDay)


class AccountPolicy(_StrictModel):
    """Per-account dispatch policy loaded from ``<config_dir>/runner-account.toml``.

    Each account owner controls their own ``max_concurrency`` and
    throttle bands by writing this file inside their Claude config
    dir. The queue-side ``[[accounts]]`` block only references the
    config_dir; the loader reads the policy here separately.

    Missing file → all defaults. Present-and-partial → unspecified
    fields fall back to defaults.
    """

    concurrency: AccountConcurrencyPolicy = Field(default_factory=AccountConcurrencyPolicy)
    throttle: AccountThrottlePolicy = Field(default_factory=AccountThrottlePolicy)


class ResolvedAccount(_StrictModel):
    """Composed view of an account: queue-side declaration + per-account policy.

    Built by :func:`config.loader.resolve_accounts` after the queue's
    ``[[accounts]]`` blocks validate. Downstream code (dispatcher,
    doctor, ``account list`` CLI) consumes this rather than
    :class:`AccountSettings` directly, so the per-account
    ``runner-account.toml`` is honoured uniformly.
    """

    name: str
    config_dir: str
    linux_user: str | None = None
    policy: AccountPolicy = Field(default_factory=AccountPolicy)


class DispatchSettings(_StrictModel):
    """Dispatch-time scope controls for the spawned ``claude`` subprocess.

    Controls how the runner builds ``--add-dir`` arguments to extend
    the sandbox the dispatched agent sees. The queue directory is
    *always* added (it contains the source files, sidecar/, and
    reports/ that the agent needs to read and write); per-task
    ``additional_dirs`` in the task YAML are always honoured. The
    optional ``auto_detect_paths_in_prompt`` toggle additionally
    scans the prompt text for absolute paths and adds the existing
    ones. The auto-detect path is off by default because absolute-path
    strings in a prompt aren't necessarily directories — false
    positives lower the precision of the safety scope.
    """

    auto_detect_paths_in_prompt: bool = False
    """Power-user opt-in: when True, extract absolute paths from the
    task's prompt text and add the ones that resolve to existing
    directories to the per-dispatch ``--add-dir`` list. Off by default."""


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
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    """Process-wide logging knobs. See :class:`LoggingSettings`. Has a
    default so existing TOMLs that pre-date this block keep parsing
    unchanged."""
    dispatch: DispatchSettings = Field(default_factory=DispatchSettings)
    plans: dict[str, PlanSettings] = Field(default_factory=dict)
    accounts: list[AccountSettings] = Field(default_factory=list)
    """One or more Claude accounts the supervisor may dispatch through.

    When unset / empty, the loader synthesises a single account named
    ``"default"`` from the legacy ``[claude].config_dir`` field so
    pre-multi-account TOMLs keep parsing unchanged. Operators with two
    or more billing identities declare an explicit ``[[accounts]]``
    block per account; see :class:`AccountSettings`.
    """

    @model_validator(mode="before")
    @classmethod
    def _synthesize_legacy_account(cls, data: object) -> object:
        """Backfill ``accounts`` from ``[claude].config_dir`` when omitted.

        Runs *before* field validation so the synthesised list is
        present when the post-validator counts names. A bare
        ``[claude] config_dir = "..."`` TOML keeps working: the loader
        sees ``accounts = []`` from the package defaults and produces
        one ``AccountSettings(name="default", config_dir=<legacy>)``.

        When the operator declares an explicit ``[[accounts]]`` list the
        legacy field is ignored entirely; the doctor emits a WARN if
        both are populated so the deprecation is visible.
        """
        if not isinstance(data, dict):
            return data
        existing = data.get("accounts")
        if existing:
            return data
        claude_cfg = data.get("claude") or {}
        legacy_config_dir = ""
        if isinstance(claude_cfg, dict):
            legacy_config_dir = str(claude_cfg.get("config_dir", "") or "")
        new = dict(data)
        new["accounts"] = [
            {
                "name": "default",
                "config_dir": legacy_config_dir,
            }
        ]
        return new

    @model_validator(mode="after")
    def _validate_account_names_unique(self) -> Settings:
        names = [acct.name for acct in self.accounts]
        if not names:
            raise ValueError(
                "at least one [[accounts]] block required (or a legacy "
                "[claude].config_dir for single-account back-compat)"
            )
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate account names: {dupes}")
        return self
