"""Pydantic settings model.

The full settings tree is built up incrementally as components land. Slices
are accessed via attribute (`settings.usage`, `settings.dispatch_pct.day`,
etc.) so a component only depends on the section it reads.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from claude_task_runner.config.duration import DurationParseError, parse_duration

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
    heartbeat_persist_interval_s: float = Field(gt=0, default=30.0)
    """Minimum seconds between in-loop ``last_heartbeat_at`` writes from
    the dispatcher. The dispatcher ticks the in-memory heartbeat on
    every stream-json event but only persists to the state YAML once
    per interval so a chatty subprocess doesn't thrash the filesystem.

    ``last_heartbeat_at`` advances *only* when the agent emits a
    stream-json event, so a healthy but agent-quiet task (long Bash
    subprocess, OAuth refresh) legitimately lets this field go stale
    past ``heartbeat_silence_alert_s``. The per-tick reaper no longer
    treats that as a hang on its own: it falls through to
    ``dispatcher_alive_at`` (always advanced by the monitor thread) and
    then a filesystem-mtime check (``zombie_verify_fs_activity_window_s``)
    before reaping. This interval therefore only bounds the staleness of
    the *active*-case freshness signal; keeping it well below
    ``heartbeat_silence_alert_s`` still gives the reaper a recent
    timestamp for tasks that are actively emitting events.
    """

    dispatcher_alive_write_interval_s: float = Field(gt=0, default=30.0)
    """Seconds between the dispatcher's monitor-thread ``dispatcher_alive_at``
    writes. Distinct from ``heartbeat_persist_interval_s`` because
    ``last_heartbeat_at`` only updates when the agent emits a stream-json
    event, while ``dispatcher_alive_at`` always advances — it proves the
    monitor thread is still pumping the subprocess pipe and the supervisor
    is alive.

    This separation lets the per-tick reaper distinguish three cases:

    * Healthy + active — both fields fresh.
    * Healthy + agent-quiet — ``dispatcher_alive_at`` fresh but
      ``last_heartbeat_at`` stale (long Bash subprocess, OAuth refresh in
      progress, etc.). The reaper treats this as HEALTHY.
    * Dead monitor — both stale. The reaper falls through to the
      filesystem activity verification step (see
      ``zombie_verify_fs_activity_window_s``).
    """

    zombie_verify_fs_activity_window_s: float = Field(gt=0, default=600.0)
    """When the per-tick reaper would mark a task SILENT/KILL based on
    the cheap heartbeat fields, it first walks the task's working_dir
    for the most recent file ``st_mtime``. If anything was modified
    within this window, the task is treated as HEALTHY (and
    ``last_heartbeat_at`` is refreshed from the mtime so the next
    pass starts from a fresh baseline).

    The walk is bounded (depth-limited, well-known noisy directories
    skipped) and runs at most once per in-flight task per reaper pass —
    only when the cheap signals already suggest a hang. Zero overhead
    when everything is healthy. Default 600s = 10 min, comfortably
    longer than a typical Bash subprocess (R package check, large
    download) but well below the default duration cap.
    """

    steady_state_reap_interval_ticks: int = Field(ge=1, default=1)
    """How many supervisor ticks elapse between two steady-state
    silent-orphan reaper runs. ``1`` (the default) runs the reaper on
    every tick — cheap because the pass only loads YAMLs for in-flight
    tasks (capped at ``sum(max_concurrency)`` across accounts).

    Operators with very long-running tasks or unusually expensive YAML
    parses can dial this back to e.g. ``4`` to reduce per-tick load;
    the trade-off is up to ``interval_ticks * poll_interval_s`` of
    extra latency before a silent subprocess is flagged.
    """


class WatchdogSettings(_StrictModel):
    restart_cooldown_s: float = Field(gt=0)
    restart_backoff_max_s: float = Field(gt=0)
    crash_loop_threshold: int = Field(ge=1)


class SupervisorSettings(_StrictModel):
    window_start_delay_s: float = Field(ge=0)
    state_file: str
    preferred_init_system: str  # auto | systemd | cron


class HookSettings(_StrictModel):
    pre_dispatch_command: str
    pre_dispatch_timeout_s: float = Field(gt=0)
    post_dispatch_command: str
    post_dispatch_timeout_s: float = Field(gt=0)


class QueueSettings(_StrictModel):
    """Queue-authoring knobs consumed by ``claude-task-runner queue add``.

    The defaults live in this Pydantic model, NOT the package TOML, so a
    queue config that pre-dates this section keeps parsing unchanged
    (extra="forbid" only rejects unknown keys, missing ones default).
    Operators opt in by adding a ``[queue]`` block to their per-queue
    ``claude_runner.toml``.
    """

    working_dir_template: str = ""
    """Template applied to a new task's ``working_dir`` when the operator
    runs ``queue add`` without ``--working-dir`` / ``--no-working-dir``.

    Supports a single ``{task_id}`` substitution (Pythonic
    ``str.format(task_id=...)``); other ``{...}`` placeholders raise a
    ``ConfigError`` at template-application time so typos surface fast.
    Empty string (the default) keeps the historical behavior: ``queue
    add`` writes ``working_dir: null`` and operators populate the field
    manually. See ADR-0023."""


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
    the loader can pull the 5h and weekly token caps for that plan.
    Empty string means "no auto-tune; use the explicit budgets."
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


# ---------------------------------------------------------------------------
# ADR-0022 — [dispatch_pct.*] variant-C trace-following.
# ---------------------------------------------------------------------------

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
"""``HH:MM`` 24-hour validator used by ``DispatchPctNight.time_*``."""


def _validate_hhmm(value: str) -> str:
    if not _HHMM_RE.match(value):
        raise ValueError(f"expected 'HH:MM' 24-hour time, got {value!r}")
    return value


def _validate_duration(value: str) -> str:
    try:
        parse_duration(value)
    except DurationParseError as exc:
        raise ValueError(str(exc)) from exc
    return value


class DispatchPctBand(_StrictModel):
    """5h-utilization thresholds for a single named band (day or night).

    The supervisor compares observed 5h utilization to these thresholds
    to pick between ``DISPATCHING`` (below ``fivehr_slowdown_pct``),
    ``SLOWING_DOWN`` (between slowdown and stop — linear concurrency
    ramp), and ``THROTTLED_5H`` (at or above ``fivehr_stop_pct``).
    See ADR-0022.
    """

    fivehr_slowdown_pct: int = Field(ge=0, le=100)
    """5h utilization at which the linear concurrency ramp begins."""

    fivehr_stop_pct: int = Field(ge=0, le=100)
    """5h utilization at which dispatch halts."""

    @model_validator(mode="after")
    def _validate_order(self) -> DispatchPctBand:
        if self.fivehr_slowdown_pct >= self.fivehr_stop_pct:
            raise ValueError(
                f"fivehr_slowdown_pct ({self.fivehr_slowdown_pct}) must be < "
                f"fivehr_stop_pct ({self.fivehr_stop_pct})"
            )
        return self


class DispatchPctNight(DispatchPctBand):
    """The night band. Adds ``time_start`` and ``time_end`` boundaries.

    The day band is the implicit complement: any local time outside
    ``[time_start, time_end)`` is daytime. ``time_start > time_end``
    means night wraps midnight (e.g. ``21:00`` → ``06:00``).
    """

    time_start: str
    """Local time-of-day (``HH:MM``) at which the night band takes over."""

    time_end: str
    """Local time-of-day (``HH:MM``) at which the night band ends and
    the day band resumes."""

    @field_validator("time_start", "time_end")
    @classmethod
    def _check_hhmm(cls, value: str) -> str:
        return _validate_hhmm(value)


class DispatchPctWeek(_StrictModel):
    """Weekly trace targets.

    The curve runs from ``0`` at week start to ``early_pct`` at the
    EOW breakpoint (located ``eow_time_switch`` before week reset),
    then from ``early_pct`` to ``eow_pct`` at week reset. The
    supervisor reads observed weekly utilization and stops dispatch
    when ``observed > target(elapsed_now)``. See ADR-0022.
    """

    early_pct: int = Field(ge=0, le=100)
    """Target utilization (%) at the start of the EOW segment."""

    eow_pct: int = Field(ge=0, le=100)
    """Target utilization (%) at week reset (end of EOW segment)."""

    eow_time_switch: str
    """Duration string (``"40h"``, ``"1d 16h"``) — the EOW segment's
    length. Anchored to the OAuth-reported weekly reset timestamp."""

    @model_validator(mode="after")
    def _validate_order(self) -> DispatchPctWeek:
        if self.early_pct >= self.eow_pct:
            raise ValueError(f"early_pct ({self.early_pct}) must be < eow_pct ({self.eow_pct})")
        return self

    @field_validator("eow_time_switch")
    @classmethod
    def _check_duration(cls, value: str) -> str:
        return _validate_duration(value)


class DispatchPctSettings(_StrictModel):
    """Composite root for the ADR-0022 dispatch-percentage policy."""

    timezone: str = ""
    """IANA timezone (e.g. ``"America/New_York"``). Empty = system local."""

    day: DispatchPctBand
    """Daytime 5h thresholds."""

    night: DispatchPctNight
    """Nighttime 5h thresholds and the local-time window that selects them."""

    week: DispatchPctWeek
    """Weekly trace target curve."""


class AccountDispatchPctBand(_StrictModel):
    """Per-account overrides for one named 5h band.

    Every field is ``T | None``: ``None`` inherits the queue-wide
    ``[dispatch_pct.<band>]`` value for that field. The composed
    :class:`throttle.policy.ResolvedPolicy` carries the merged values.
    """

    fivehr_slowdown_pct: int | None = Field(default=None, ge=0, le=100)
    fivehr_stop_pct: int | None = Field(default=None, ge=0, le=100)


class AccountDispatchPctNight(AccountDispatchPctBand):
    """Per-account overrides for the night band, including time window."""

    time_start: str | None = None
    time_end: str | None = None

    @field_validator("time_start", "time_end")
    @classmethod
    def _check_hhmm(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_hhmm(value)


class AccountDispatchPctWeek(_StrictModel):
    """Per-account overrides for the weekly trace curve."""

    early_pct: int | None = Field(default=None, ge=0, le=100)
    eow_pct: int | None = Field(default=None, ge=0, le=100)
    eow_time_switch: str | None = None

    @field_validator("eow_time_switch")
    @classmethod
    def _check_duration(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_duration(value)


class AccountDispatchPolicy(_StrictModel):
    """Per-account dispatch_pct overrides composed onto queue-wide settings."""

    timezone: str | None = None
    day: AccountDispatchPctBand = Field(default_factory=AccountDispatchPctBand)
    night: AccountDispatchPctNight = Field(default_factory=AccountDispatchPctNight)
    week: AccountDispatchPctWeek = Field(default_factory=AccountDispatchPctWeek)


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
    dispatch_pct: AccountDispatchPolicy = Field(default_factory=AccountDispatchPolicy)


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
    """Per-plan token budgets.

    ADR-0022 simplified this block to token caps only; dispatch shape
    lives in ``[dispatch_pct.*]`` which the operator sets directly.
    """

    five_hour_tokens: int = Field(gt=0)
    weekly_tokens: int = Field(gt=0)


class Settings(_StrictModel):
    """Root settings model — the merged effective configuration."""

    claude: ClaudeSettings
    usage: UsageSettings
    dispatch_pct: DispatchPctSettings
    """ADR-0022 ``[dispatch_pct.*]`` tree. Variant-C trace-following
    dispatch policy."""
    concurrency: ConcurrencySettings
    ema: EMASettings
    effort_levels: dict[str, list[str]]
    session: SessionSettings
    failure_classifier: FailureClassifierSettings
    task_caps: TaskCapsSettings
    watchdog: WatchdogSettings
    supervisor: SupervisorSettings
    hooks: HookSettings
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    """Process-wide logging knobs. See :class:`LoggingSettings`. Has a
    default so existing TOMLs that pre-date this block keep parsing
    unchanged."""
    dispatch: DispatchSettings = Field(default_factory=DispatchSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    """``[queue]`` block — task-authoring knobs consumed by ``queue add``.
    Has a default so existing TOMLs that pre-date this block keep
    parsing unchanged. See :class:`QueueSettings` and ADR-0023."""
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
