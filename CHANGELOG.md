# Changelog

Notable changes to `claude-task-runner`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is pre-1.0 — minor versions can introduce breaking changes.
Breaking changes are called out in the version notes.

## [Unreleased]

### Removed

- **Settings that no code ever read are gone, and a queue TOML that still
  sets one no longer loads (breaking).** Every settings model is
  `extra="forbid"`, so each of these loaded without complaint and did nothing:
  - `[claude].plan` and the `[plans.*]` token budgets. They were staged on
    2026-05-11 for loader auto-tuning that never came, and ADR-0022 made them
    moot: the throttle compares the utilization percentages `/usage` reports,
    already relative to the account's tier, against `[dispatch_pct.*]`.
    `docs/first-time-setup.md` put `plan = "max20x"` in every new queue.
  - `[ema]` (`alpha`, `prior_warmup_samples`, `runtime_p90_multiplier` and the
    `[ema.priors.*]` tables), the EMA of ADR-0011, now deprecated. No
    production code ever called `update_bucket`, so no queue ever had an
    `ema.json`, and the predictions' only caller was an end-of-week-push check
    that nothing called. `runner/ema.py`, `runner/runtime_stats.py` and the
    doctor's `ema` check are gone too. Concurrency never depended on the EMA:
    `initial_concurrency` holds until a first task completes, then
    `max_concurrency`.
  - `[usage].suspicious_delta_pct`, together with
    `usage.drift.validate_monotonicity` and `UsageMonotonicityDrift`, which
    nothing called or raised. Key invariant 3 in `docs/architecture.md` said
    a utilization decrease without a detected reset "is `UsageFormatDrift`",
    under a heading saying tests and assertions enforce every invariant. It
    is retired in place, because code comments cite invariants by number. The
    check was removed rather than wired in because a false alarm would halt
    dispatch. For example, the API source rounds the header's fraction to a
    whole percent while the TUI prints its own whole number, so an
    `api_then_tty` fallback can read a point lower than the poll before it.
  - `[usage].healthcheck_interval_s`, the period of a background healthcheck
    that nothing ever scheduled. `claude-task-runner usage healthcheck` still
    runs one on demand.
  - `[session].resume_fail_fast_s`, with `runner.session.fall_through_to_fresh`
    and `dispatch()`'s `settings_session` parameter. The fast fall-through
    ADR-0005 describes was never built. Every `--resume` counts toward
    `[session].max_resume_attempts`, whatever its outcome, and at the cap
    dispatch goes fresh. `TestResumeAttemptCounting` now pins that.

  **Migration:** the loader rejects any of these keys in a `claude_runner.toml`
  with one `ConfigError` that lists each retired key in the file and why it
  went. Delete them. None was ever read, so deleting them changes nothing, and
  it is safe to do before upgrading, while the old runner is still running.
  README, the architecture doc, the cheat sheet, first-time setup, the runbook,
  the `runner-add-task` skill and ADRs 0005, 0011 and 0022 no longer describe
  any of them as live.
- **A gate so that cannot recur.** `tests/unit/test_settings_readers.py` walks
  every field reachable from `Settings` and `AccountPolicy` and fails when no
  runtime code reads its name. It scans with `ast`, so docstrings and comments
  do not count. Reads in the schema's own helper methods count; validators'
  reads do not. It found every setting above, including
  `[session].resume_fail_fast_s`, which a text search had missed because
  docstrings spell it. Its allowlist is empty.
- **The docs-vs-schema gate handles retired keys.**
  `tests/unit/test_docs_config_refs.py` can allowlist a single retired field
  (`claude.plan`) as well as a whole table, and it requires a loader guard
  that names each allowlisted key, so an operator following a stale mention is
  told to delete it. It also now reads a bracketed reference with a
  `<placeholder>` segment, such as `[ema.priors.<model>.<effort>]`, which it
  used to skip.

### Changed

- **Queue YAML is parsed with LibYAML's `CSafeLoader` when PyYAML has it,
  so a tick's `todo/` scan is about 11× faster.** Every supervisor tick,
  `_eligible_candidates` and `planned_dispatch_order` load every task YAML in
  `todo/`, and `queue/store.py` parsed them with PyYAML's pure-Python
  `SafeLoader`. On the 5,294-task nlmixr2lib queue (read-only, best of 3), a
  full `load_task` pass went from 14.6 s to 1.3 s. Parsing alone went from
  14.4 s to 1.1 s; pydantic validation is 0.04 s. `_load_yaml` is the only
  YAML parse site in `src/`, so every reader of task and state files (the
  orchestrator, reconcile, adoption, doctor and the CLI) gets the speedup.
  A PyYAML built without LibYAML falls back to `SafeLoader`.

  Both loaders parsed all 5,294 task and 4,623 state files of that queue to
  identical objects, type for type. They share PyYAML's resolver and
  constructor, and their scanners differ only on hand-written edge cases,
  each now pinned by a test. `CSafeLoader` accepts a tab as separating
  whitespace (`key:<TAB>value`, a trailing tab), which `SafeLoader` rejected,
  so a hand-written task skipped as unparseable for that reason alone now
  loads and becomes dispatchable. In the other direction, it rejects escaped
  surrogates (`"\ud83d"`), `%YAML` versions other than 1.1/1.2 and unknown
  `%` directives, which `SafeLoader` accepted. The runner's own writers never
  emit any of these, and no file in that queue parses differently.

### Removed

- **Ten helpers that nothing in production called.** `runner.readiness.is_ready`
  wrapped the live `unmet_requirements`. In `queue.sidecar`, `write_request` and
  `next_sequence` duplicated what the agent does itself under the
  `agent-stop-and-ask` skill, and `read_response` and `outstanding_question_ids`
  duplicated the live readers, which work on raw payloads.
  `runner.effort_levels` lost `accepted_efforts` and `accepted_models`, since
  `queue add` calls `validate_effort`. `UnknownModel` went with them: only
  `accepted_efforts` raised it, so the `unknown model:` branch in `queue add`
  could never run. `queue add` still reports an unknown model as `invalid
  effort: model ... has no effort levels configured`.
  `runner.heartbeat.silence_window`, `runner.session.claude_session_jsonl` and
  `cli.queue_cmd._emit` had no caller at all. Tests now write sidecar requests
  with a helper under `tests/unit/`, and the task-template drift guard moved
  into its test.

- **Six empty packages, the `ui` extra and the Jinja2 task-templates
  promise.** `events/`, `logs/`, `metrics/`, `notify/`, `templates/` and `ui/`
  each held only an empty `__init__.py` from the initial commit, and nothing
  imported them. The `ui` extra installed `textual` for a terminal UI that was
  never built, and nothing imports `textual`. The install lines in the README,
  `docs/first-time-setup.md` and CI now use `.[dev]`. An old `.[dev,ui]`
  still installs, and pip and uv only warn that the extra is gone.
  `docs/first-time-setup.md` also no longer says the `dev` extras carry the
  doctor's dependencies; the doctor needs none of them.
  `docs/architecture.md` no longer tells operators to drop Jinja2
  task templates into a `templates/` directory. Nothing reads one, Jinja2 is
  not a dependency, and ADR-0023 rejected a template engine. The wheel's
  `force-include` entry for `templates/` goes with the package.
- **The unused `supervisor/window.py` module and its tests.** No module
  imported it, at module level or inside a function, so neither the CLI nor
  the supervisor daemon nor the runner could reach it. Its contents either
  live elsewhere or belonged to removed behavior. `schedule_window_start_wakeup`
  duplicated `throttle.decision._next_5h_reset_wakeup`, which is what actually
  schedules the wakeup after a 5-hour reset; that path is unchanged.
  `in_eow_push_window` served the end-of-week push that ADR-0022 removed.
  `crossed_reset`, `crossed_reset_5h` and `crossed_reset_weekly` were the reset
  detection for `usage.drift.validate_monotonicity`, which nothing called and
  which is now removed along with `[usage].suspicious_delta_pct`.
  `time_until_reset_s` had no caller, and the module's `FIVE_HOUR_LENGTH_S` and
  `SEVEN_DAY_LENGTH_S` duplicated `throttle.decision.FIVE_HOUR_LENGTH_S` and
  `throttle.curve.SEVEN_DAYS_S`. No setting, command or file format changes.

- **`force_dispatch_in_eow`, a task field nothing read.** It overrode the
  end-of-week push's runtime guard, and ADR-0022 removed that push. `Task`
  rejects unknown keys, so `load_task` now drops this one from an existing task
  YAML instead of refusing the file, and logs a warning naming the file (once
  per file per process, since every tick reloads every task). A task that set
  it dispatches exactly as before. `queue template` no longer lists it, and the
  `runner-add-task` skill no longer names it as an example.

### Fixed

- **The systemd unit now takes its restart policy from the queue's
  `[watchdog]`.** `install` wrote `RestartSec=30`, `StartLimitBurst=5` and
  `StartLimitIntervalSec=600` into the unit whatever the queue's
  `claude_runner.toml` said, so its `[watchdog]` table did nothing under
  systemd. The unit now gets `RestartSec` from
  `[watchdog].restart_cooldown_s`, `StartLimitBurst` from
  `[watchdog].crash_loop_threshold` and `StartLimitIntervalSec` from
  `[watchdog].restart_backoff_max_s`. The package defaults are 30 s, 5 and
  600 s, so a queue that does not set them gets the same unit as before,
  byte for byte, and a test pins that. To apply a changed `[watchdog]` to an
  installed unit, re-run `claude-task-runner install`. It rewrites the unit
  and reloads systemd, and the next crash uses the new values without the
  supervisor being restarted. That was checked on systemd 255 with a
  throwaway unit. Once systemd has started the unit more than
  `crash_loop_threshold` times within `restart_backoff_max_s`, it stops
  restarting it. Unlike the cron watchdog, it does not back off and retry;
  `systemctl --user reset-failed claude-task-runner` clears the limit.
  Seconds are written as decimals rounded to systemd's one-microsecond
  resolution, never in exponent form: Python writes `1e-05`, which systemd
  cannot parse. systemd ignores a unit line it cannot parse, with only a
  journal warning, and falls back to its own default (`RestartSec=100ms`).
  So a value systemd cannot parse now stops `install` with exit 2 before
  anything is written: an infinite span (the schema's `> 0` check lets
  `inf` through), a span longer than 18,446,744,073,708 s, or a
  `crash_loop_threshold` above 4,294,967,295. Where `systemd-analyze` is
  installed, tests run the generated unit through `systemd-analyze verify`
  and the written spans through `systemd-analyze timespan`. A known-answer
  test checks that `verify` does report an ignored line. A test that walks
  `WatchdogSettings` fails when a `[watchdog]` key has no unit line.
  `build_unit_text` and `build_install_plan` now require a `watchdog`
  argument and no longer take `restart_sec_s`, `start_limit_burst` or
  `start_limit_interval_s`.

  **The cron watchdog still ignores a queue's `[watchdog]`.** `watchdog.sh`
  runs `watchdog tick` with no `--config`, so the tick uses the package
  defaults, and a cron `install --config` is not recorded. Tests pin both
  until a follow-up makes the tick load the managed queue's config.
- **A relative `install --config` now reaches the systemd unit as the file
  `install` checked.** `install --config rel.toml` loaded `rel.toml` from
  the directory it ran in, but wrote `--config rel.toml` into the unit's
  `ExecStart` and `ExecStop` as given, and the unit runs with
  `WorkingDirectory=<queue>`. So the supervisor read `<queue>/rel.toml`. That
  file was usually missing, so every start failed until the start limit
  stopped the restarts, and at best it was not the file `install` had
  checked. `install` now makes the path absolute before loading it. If
  `systemctl --user cat claude-task-runner` shows a relative `--config`,
  re-run `claude-task-runner install` from the directory that path is
  relative to.
- **`--help` no longer drops bracketed words such as `[queue]` and
  `list[str]`.** Typer's default `rich_markup_mode` is `"rich"`, which parses
  every help string as Rich console markup. Rich takes `[` followed by a
  lowercase letter as the start of a style tag and deletes the tag, so ten
  help texts in nine commands lost text. `supervisor drain --help` showed
  `[task_caps].max_duration_s_per_task` as `.max_duration_s_per_task`,
  `queue restart-fresh --help` showed `[[accounts]]` as `[]`, and
  `sidecar answer --help` showed `list[str]` as `list`. Every `typer.Typer`
  in `cli/` now passes `rich_markup_mode=None`, so help prints exactly as
  written. It is now in click's plain format rather than Rich panels, and
  usage errors print as click's plain `Error:` lines. `console.print`
  output keeps its colours. Click re-wraps help paragraphs, so the two
  "Exit codes:" tables and the bullet lists in `supervisor start` and
  `queue force-dispatch` now open with click's `\b` no-rewrap line.
  `tests/unit/test_docs_cli_help.py` renders every command's `--help`. It
  fails when a bracketed token in the help text is missing from the output,
  when any `typer.Typer` in the tree uses a markup mode, or when a laid-out
  paragraph has no `\b`. Known-answer tests pin the checker: under the old
  mode it reports exactly the tokens Rich drops, both on a demo app and on
  the real CLI.
- **`--help` shows the default of `--queue` as `(current directory)`, not a
  Python repr.** Every command that takes `--queue`, 22 of the 44 help pages,
  printed `[default: <bound method Path.cwd of <class 'pathlib.Path'>>]`
  (the names in it vary with the Python version). Each `--queue` passes the
  method `Path.cwd` as its default, so that click calls it when the command
  runs, and typer shows a callable default that is not a plain function with
  `str()`. Each now also passes `show_default=CWD_DEFAULT_LABEL`, which
  `cli/_helpers.py` defines once. The default itself is unchanged: the
  directory the command runs in. `tests/unit/test_docs_cli_help.py` now fails
  when any command's `--help` shows a default holding a Python repr
  (`<bound method`, `<function`, `<class` or `<built-in`). Because the label
  changes only the help, it also checks that every `--queue` still parses to
  the directory the command runs in, so a default fixed at import would fail
  even though help still said `(current directory)`. Known-answer tests pin
  both checks: the old declaration is flagged and the new one is not, and a
  demo `--queue` whose default is fixed at import is caught.
- **A cron watchdog tick no longer recreates a registered queue that was
  deleted or moved.** `register_queue` rejects a path that is not a
  directory, but only when it registers it. For a queue that was later
  deleted, moved or replaced by a file, every tick found no live supervisor
  and approved a restart, and `_spawn_supervisor` made
  `<queue>/.claude_task_runner` with `parents=True`, which recreated the
  queue directory. The supervisor started on that empty queue held the
  per-user `global.lock`, so the operator's real queue failed with
  `another supervisor is already running`. Every queue shares one restart
  history in `watchdog_state.json`, so a missing queue listed first also took
  the restart and left the real queue in cooldown on every tick. A tick now
  checks each registered path before it decides anything. For a path that is
  not an existing directory it writes
  `watchdog: ERROR queue=<path> is not an existing directory, ...` to
  `~/.claude_task_runner/watchdog.log`, with the command that unregisters it,
  and records no restart. The entry stays registered, so a queue on a
  filesystem that was not mounted is managed again once it is.
  `watchdog queues` warns on stderr about each such path, and its stdout is
  still one path per line. `_spawn_supervisor` no longer passes
  `parents=True`, so a queue deleted between the check and the spawn is not
  recreated either. `queues.json` is now written to a temporary file and
  renamed into place, so a tick never reads half a registry. With only the
  cron block installed, `doctor`'s `watchdog_installed` check now WARNs about
  every registered path that is not an existing directory and prints the
  `claude-task-runner watchdog unregister --queue <path>` for each. The
  runbook has a section for the symptom, including how to stop a supervisor
  that an older version already started on a recreated queue.
- **The cron watchdog manages one queue, so it no longer fights the per-user
  lock.** Only one supervisor runs per user, because each takes
  `~/.claude_task_runner/global.lock`. Yet a cron `install` or
  `watchdog register` for a second queue added it to
  `~/.claude_task_runner/queues.json` beside the first, and a tick managed
  every queue listed. While one queue's supervisor held the lock, every tick
  spawned the other queue's, and each spawn exited 2 with
  `another supervisor is already running`. Each refused spawn counted toward
  the crash-loop threshold that all queues shared in `watchdog_state.json`,
  so a real crash of the running queue could be held in BACKOFF: one extra
  minute with the default settings, three with `restart_cooldown_s = 60`.
  Registry order also picked the winner. With `[B, A]` and A running, a
  crash of A started B's supervisor and refused A's from then on, so a
  second `install` never took effect.

  Now, as with the single systemd unit, a cron `install` and
  `watchdog register` replace the registered queue, and each names the
  queue it replaces. A tick manages the last queue in `queues.json`, so a
  file that an older version let grow keeps working: the tick ignores the
  other entries and logs a `WARNING` naming them, `watchdog queues` warns
  about them on stderr, and doctor's `watchdog_installed` check warns and
  gives the fix. When the managed queue's supervisor is down but another
  process holds `global.lock`, the tick logs the new `verdict=locked`,
  starts nothing and counts no restart, so the queue starts on the first
  tick after the lock frees. The tick asks the lock itself, with a
  non-blocking `flock`, because the PID left in the file outlives its
  holder. `install` and `watchdog register` say when the replaced queue's
  supervisor still holds the lock, and give the
  `claude-task-runner supervisor drain --queue <old-queue>` that hands
  over. The restart history in `watchdog_state.json` now names its queue,
  and a tick that finds another queue registered starts it empty, so one
  queue's restarts never hold back another's. The runbook has a section
  for `verdict=locked`. Tests replay the old failures on a simulated cron
  clock against the real lock, and temporary mutants (append instead of
  replace, ignore the lock, keep the history across a switch, trust the
  lock file's PID, manage the first entry) each fail them.
- **`watchdog tick --dry-run` no longer records a restart.** A dry run saved
  the restart it approved, so the next real tick counted a restart that
  never happened toward the cooldown and the crash-loop threshold: a real
  tick a second after a dry run gave `verdict=cooldown`. A dry run now
  decides and logs, but saves no state.
- **doctor's `global_lock` check asks the lock, not the PID left in it.**
  The file keeps the last holder's PID after it exits, so doctor warned
  that the lock was stale after every supervisor stop and suggested
  removing it, and a PID that the OS had since given to another process
  read as a running supervisor. doctor now probes the lock with `flock` and
  reports it free, or held with the holder's PID. A leftover file is
  harmless, since the next supervisor locks the same file. Removing it
  while a supervisor holds the lock is what would let a second one run
  beside it, so the runbook's crash-loop section no longer suggests it.
- **`supervisor start`, `install`, `queue add` and `queue force-dispatch`
  refuse a `--queue` that is not an existing directory.** They used to
  create it, because `queue_runtime_dir()` and `todo_dir()` make their
  directories with `parents=True`. A mistyped or deleted `--queue` became an
  empty queue, and a `supervisor start` on it held the per-user
  `global.lock`, so the real queue's supervisor failed with
  `another supervisor is already running`. Each command now exits 2 with
  `--queue is not an existing directory: <path>` before it loads settings,
  shows a plan or writes anything. `queue force-dispatch --json` prints that
  as `{"ok": false, "error": ...}`. `install` checks before it detects the
  init system. Its cron branch used to show the crontab diff and ask to
  confirm before failing to register the queue, and its systemd branch wrote
  and started a unit whose `WorkingDirectory=` did not exist. The watchdog
  spawns `supervisor start`, so the check there also covers a queue deleted
  between a tick's check and the spawn. All of them, and
  `watchdog register`, use `queue.store.require_queue_dir()`. Like the tick,
  it treats a path it cannot examine, such as one under a directory the user
  may not search, as missing instead of raising `PermissionError`. `--queue`
  still defaults to the current directory, and the documented setup creates
  the queue directory first, so neither is affected.
- **Under systemd, a supervisor that exits cleanly now leaves the unit
  `inactive (dead)` instead of `failed`.** systemd runs the unit's
  `ExecStop` even when the supervisor has already exited on its own: after
  `kill <pid>`, `claude-task-runner supervisor stop`, a drain, or on
  reaching the STOPPED state. By then the supervisor has removed its PID
  file, so the `ExecStop` command (`supervisor stop`, or
  `supervisor drain --no-wait` when `[supervisor].adopt_workers` is off)
  printed `No PID file` and exited 1. systemd logged
  `Failed with result 'exit-code'` and left the unit
  `failed (Result: exit-code)`, so
  `systemctl --user is-failed claude-task-runner` reported true after every
  clean stop. The supervisor was not restarted, because
  `RestartPreventExitStatus=0` matched its exit 0. The same exit 1 also
  made `Restart=on-failure` restart a supervisor killed by a signal that
  systemd counts as clean (SIGHUP, SIGINT, SIGTERM or SIGPIPE). That
  restart came from the failed `ExecStop`, not from how the supervisor
  exited. The generated unit now writes `ExecStop=-...`, and the `-` tells
  systemd to ignore the command's exit status. `ExecStart` has no prefix,
  so a supervisor that fails still counts as failed, and a crash (a
  SIGKILL, for example) still restarts it after `RestartSec`. This was
  checked on systemd 255 by running the generated unit text as transient
  user units, with the real `supervisor stop` and `supervisor drain` as
  `ExecStop`. A unit installed before this change keeps its old `ExecStop`
  until you re-run `claude-task-runner install`. Step 3 of the runbook's
  "Cron / systemd watchdog not installed" now describes both kinds of unit.

- **The runbook gives a safe way to test the systemd restart.** Step 3 of
  "Cron / systemd watchdog not installed" said systemd restarts the
  supervisor after a crash but gave no way to check it, and the obvious
  tests mislead. `kill <pid>` and `supervisor stop` send SIGTERM; the
  supervisor exits 0 and `RestartPreventExitStatus=0` leaves it down. A
  unit installed before the ExecStop fix above also shows
  `failed (Result: exit-code)`, because its ExecStop runs after the
  supervisor has gone and exits 1.
  `systemctl --user kill --signal=KILL` does crash it, but its default
  `--kill-whom=all` also SIGKILLs every in-flight `claude` worker in the
  unit's cgroup. The step now uses
  `systemctl --user kill --kill-whom=main --signal=KILL claude-task-runner`
  (`--kill-who=main` before systemd 252, or `kill -KILL` on the PID from
  `supervisor status`), and says what the restart looks like in the
  journal. The behaviour was checked on
  systemd 255 with transient units that use the unit's settings: SIGKILL
  of the main process restarted it with the workers alive, and the
  default form killed them.

- **A cron `install` now registers its queue, so the cron watchdog restarts
  the supervisor.** The crontab line runs `watchdog.sh`, which runs
  `claude-task-runner watchdog tick` with no `--queue`, and a tick manages
  only the queues listed in `~/.claude_task_runner/queues.json`. Nothing but
  `watchdog register` wrote that file, and no doc mentioned that command.
  Its `--help` even said `install` called it. So after a cron install every
  tick logged `watchdog: no queues registered; nothing to do`, and a dead
  supervisor stayed dead. `install` now registers `--queue` once you confirm,
  and before it touches the crontab, so a failed registry write leaves
  nothing changed. The confirmation lists the registry change under the
  crontab diff. The systemd path registers nothing: systemd restarts its own
  unit, and a registered queue would let a cron tick restart a supervisor
  that the unit left stopped on purpose. Registering a path that is not an
  existing directory is now an error. Without that check, the next tick's
  restart would create a mistyped queue directory and start a supervisor on
  it, which would hold the per-user global lock. Tests pin the path from
  `install` to the tick's `verdict=restart`.

  **Existing cron installs still have an empty registry.** Run
  `claude-task-runner watchdog register --queue <queue>`, or re-run
  `install`. `claude-task-runner watchdog queues` lists what is registered.
  Because the next tick now starts the supervisor within a minute of a cron
  install, `docs/first-time-setup.md` and the README quick start no longer
  say to run `supervisor start` after `install`. The runbook has a section
  for the empty-registry symptom. Docstrings that described a systemd timer
  running `watchdog tick` now say that nothing on the systemd path runs one.
- **`doctor` no longer passes a cron watchdog that does not manage the
  queue.** Its `watchdog_installed` check reported "cron watchdog detected"
  whenever the crontab had the managed block, so it passed every cron
  install that the fix above does not repair. With only the cron block
  installed, it now PASSes only when `~/.claude_task_runner/queues.json`
  lists the queue. Otherwise it WARNs and gives the
  `claude-task-runner watchdog register --queue <queue>` to run. A corrupt
  registry gets its own WARN. doctor reads the registry without side
  effects, so the file is left as it is and no `queues.json.broken` copy is
  made, as a watchdog tick would. A systemd unit still PASSes without the
  registry being read. The registry code moved from `cli/watchdog_cmd.py`
  to a new `cron/registry.py`, so doctor does not import from the CLI.
  There, `read_registered_queues()` raises `RegistryError` on a corrupt
  file, and the tick's `load_registered_queues()` keeps treating one as
  empty. A registry whose `queues` value is not a list now counts as
  corrupt too: the tick logs it and backs it up, where it used to be read
  as empty with no trace.
- **The docs no longer send operators to a `drift.log` that nothing
  writes.** `docs/architecture.md`'s per-queue tree listed
  `<queue>/.claude_task_runner/drift.log` ("parser drift + healthcheck
  results"), `docs/cheatsheet.md` said to `tail -F` it for drift and
  capture failures, and the runbook's drift symptom said it "has recent
  entries". No commit has ever written it, and the periodic runtime
  healthcheck whose results it was to hold was never built. The same
  symptom line said a desktop notification fires. None does: the
  `[notify]` backends were deleted as dead config on 2026-06-13, and
  `supervisor start` wires no notifier, so a `Notify` action is only an
  INFO log line. The docs now name the evidence that does exist.
  `supervisor status` shows state `error_drift` and a `Last drift:` line,
  from `last_drift_message` in `supervisor.json`. The supervisor log has
  one `notify[error]: parser drift: ...` line. With the TTY usage source,
  the capture that failed to parse is the newest `usage_captures/<ts>.cap`.
  The architecture doc and cheat sheet also said the `EmitEvent` actions
  (`drift_detected`, `state_transition`, `usage_capture_error`, ...)
  reach the supervisor log. They are logged at DEBUG only, below the
  default INFO, so a usage capture that times out leaves no trace at INFO.
  A new "Supervisor log and drift evidence" section in the architecture
  doc says so, and says where the log goes: journald under the systemd
  unit, and `<queue>/.claude_task_runner/supervisor.log` only when the cron
  watchdog started the supervisor.
- **The rest of the on-disk layout matches the code.** `banner.txt` is
  gone from the per-queue tree: nothing ever wrote it either, and its only
  source was the deleted `[notify].file_path`. `watchdog.log` moves to the
  global tree, because `cron/watchdog.sh` writes
  `~/.claude_task_runner/watchdog.log`, and the runbook's crash-loop and
  watchdog-install steps now give its full path, with the `journalctl`
  equivalent under systemd. The global tree gains `queues.json`,
  `watchdog_state.json` and the `usage_captures/` that the `usage` CLI
  commands write. The per-queue tree gains `state/.corrupt/` (ADR-0028)
  and `force_dispatch/`. The cheat sheet's supervisor-log row pointed only
  at `supervisor.log`, which does not exist under the systemd unit.
- **A docs-vs-code gate for the on-disk layout.**
  `tests/unit/test_docs_disk_layout.py` parses both trees in
  `docs/architecture.md` and fails on any entry whose name no code spells
  out. A name counts when it appears in a string literal in a `.py` file
  under `src/claude_task_runner/` (docstrings excluded), or in a value or
  code line of a `.sh` or `.toml` file there (comments excluded).
  `skills/` is not searched. Templated names are split at their
  placeholders, so `request-NNN.json` needs both `request-` and `.json`.
  Run against the old tree, it reports exactly `drift.log` and
  `banner.txt`. Known-answer tests pin the parser, and a missing section,
  an empty tree, or an entry that is not a single path fails the gate
  instead of dropping out of it. It checks names, not placement, so it
  would not have caught `watchdog.log` in the wrong tree.
- **A deeply nested queue YAML is now a `QueueSchemaError` instead of a crash
  (`MAX_YAML_DEPTH = 64`).** Both loaders recurse once per nesting level.
  `CSafeLoader` overflows the C stack and segfaults at about 26,000 levels,
  which is a 26 KB file and far under `MAX_YAML_BYTES`. Unbounded, one such
  file in `todo/` would have killed the supervisor on every tick. Under
  `SafeLoader`, the same file raised `RecursionError` from about 490 levels
  on, and that escapes every `except QueueSchemaError`: a 2 KB task nested
  1,000 levels deep made `claude-task-runner queue list` crash with a
  traceback, hiding every other task. Depth is now counted while composing,
  under either loader, and a deeper document is rejected with its location.
  The deepest schema-valid document is 5 levels, so no file that could
  validate is affected.

### Added

- **`claude-task-runner watchdog unregister --queue <queue>` drops a queue
  from the cron watchdog's registry.** Removing an entry from
  `~/.claude_task_runner/queues.json` used to mean editing the file by hand;
  `install uninstall` removes the crontab block and leaves the registry
  alone. `unregister` works whether or not the directory still exists. It
  matches an entry both as written and as resolved, so a path copied from
  `watchdog queues` or `watchdog.log` removes its entry even when a symlink
  on it has changed since. It is idempotent: for a queue that is not listed
  it prints `not registered: <path>` and exits 0. A corrupt registry makes
  it exit 2 and is left as it was. The tick's lenient reader would treat
  that file as empty, and rewriting it would drop every other queue.
  `--queue` defaults to the current directory, as it does for
  `watchdog register`. `install uninstall` still leaves the registry alone,
  but once no cron block is installed it prints the queues the registry
  still lists, each with the `unregister` command that drops it, because a
  later cron `install` would manage all of them again. It stays silent when
  the operator keeps the cron block, since the watchdog is still using those
  queues, and when `crontab -l` cannot be read. A corrupt registry gets a
  warning and is left as it is.
- **`claude-task-runner worktree reclaim` removes finished tasks' worktrees
  (ADR-0034).** A queue whose pre-dispatch hook creates one git worktree per
  task accumulated them forever. On 2026-09-25 the nlmixr2lib queue had 305
  of them holding 36 GB, and 244 belonged to tasks that were long finished
  and merged. A worktree is removed only when its task is `completed` and not
  in flight, and its branch is checked out there and is an ancestor of
  `<remote>/<parent_branch>` after a fetch. Its `git status` must also be
  clean, except for untracked paths in `discardable_untracked` (default
  `tests/testthat/_problems/`), which are discarded with `--force`. The local
  branch goes with `git branch -d`, never `-D`. The command is a dry run
  unless given `--apply`. It lists every kept worktree with the condition it
  failed, and it takes the hook's `flock` around the fetch and each removal
  when `lock_file` is set.
- **Opt-in periodic reclaim from the supervisor.** With
  `[worktree_reclaim].periodic = true`, the supervisor runs the same pass on
  its first tick and every `interval_s` after that, at most `max_per_pass`
  removals at a time and never during drain. Removals are emitted as
  `worktree_reclaimed` events; failures raise a warning notification.
- **`merge_branches.sh` aborts on same-path collisions.** Two branches that each add a
  file at the same path under `inst/modeldb/` or `vignettes/articles/` with
  different content cannot both survive `-X theirs`; the survey now lists them and
  exits 4 (dry runs included) so one branch can be relettered or excluded. On
  2026-09-24 two different papers had both been added as `Wang_2019_tacrolimus`
  and one model silently vanished.
- **`merge_branches.sh --exclude-ref <ref>`** (repeatable) leaves a branch out of a
  consolidation even though `--pattern` matches it, printing each exclusion in
  the survey; a WIP task checkpoint or a branch with its own PR no longer forces
  a narrower glob or a temporary branch deletion.

- **A terminal close now writes its own dispatch gate (ADR-0033).** A run
  that ends as a clean skip or defer writes a deliverable, commits nothing
  and leaves the worktree clean; ADR-0020 marks that `completed`, which is
  not dispatchable, so on its own it never re-fires. But a task that filed a
  sidecar sits in `awaiting_sidecar`, and answering every request makes it
  eligible again — correct when there is a ruling to act on, pure waste when
  the disposition was terminal. The task then re-derives the same verdict at
  full effort and files the same sidecar. Observed on the nlmixr2lib queue:
  `oare_PMC6930853` was acked as a skip and re-fired 24h later at
  `effort: high` reaching the identical verdict; `oare_PMC9823018` did the
  same. The dispatch selector reads only the `block_dispatch` register, so a
  row there is the only thing that holds such a task down, and until now
  nothing wrote one except an operator by hand.

  `_finalize_state` now appends that row itself when the ADR-0020 evidence
  shows the unambiguous terminal shape. The status is unchanged — a terminal
  close is a genuine completion, and flipping it to `failed` would be worse,
  since `failed` IS dispatchable. A run that committed is not gated (that
  would strand real work), and a run that left work uncommitted is not gated
  (it must be re-dispatched to finish). An existing row is never overwritten
  or duplicated, so a curated operator ruling always wins; auto-written rows
  carry `status: AUTO_GATED` so they can be audited or reversed in bulk; and
  any write failure is swallowed, because a register problem must never fail
  a run that genuinely succeeded. No-op when `[dispatch].dispatch_block_file`
  is unset.

### Fixed

- **The ADR index lists ADR-0033.** `docs/decisions/README.md` stopped
  at 0032: ADR-0033 (a terminal close writes its own dispatch gate)
  landed on 2026-09-04 without its row, and only a sentence of prose
  asked for one. `tests/unit/test_docs_adr_index.py` now fails when an
  ADR file has no index row, a row has no ADR file, a number is used
  twice (two branches can each claim the next free number), or a file
  under `docs/decisions/` is not named `NNNN-<slug>.md` and so would
  escape those checks. Known-answer tests pin the row parser, and a row
  it cannot read fails the gate rather than dropping out of it.
- **README and the cheat sheet quote the coverage gate CI enforces
  (90%).** CI raised `--cov-fail-under` from 75 to 90 on 2026-05-16, but
  the "pipeline that CI runs" in `README.md` still ran
  `--cov-fail-under=75` — so it could pass locally where CI failed — and
  `docs/cheatsheet.md` still called 90% "aspirational". The cheat-sheet
  section is rewritten as "Coverage gate" and drops its claim that a
  live-test suite (`CTR_RUN_LIVE_TESTS=1`) covers `usage/capture.py`: no
  test carries the `live` marker, so `capture()` is simply uncovered.
  `tests/unit/test_docs_coverage_gate.py` now fails when README's gate
  differs from the one in `.github/workflows/ci.yml`, which it reads
  from the workflow's parsed `run:` steps so a gate quoted in a comment
  cannot stand in for it. Known-answer tests pin both parsers, and a
  missing README block or a CI file with zero or two gates fails rather
  than comparing nothing.
- **The cheat sheet's "Add a new plan" recipe now works.** Its last step
  ran a `supervisor` subcommand that does not exist and failed with
  `No such command 'restart'`. Reading the code showed the recipe was
  wrong in more places than that. `[claude].plan` and `[plans.*]` are
  schema-validated but read by no runtime code, so changing them needs
  neither a reload nor a restart. What does matter is the account switch
  (`[claude].config_dir` or `[[accounts]]`), and that needs a real
  restart: `supervisor drain` then `supervisor start` (or the cron
  watchdog), or `systemctl --user restart claude-task-runner` under
  systemd. A SIGHUP reload is not enough, because the `/usage` poller is
  built once at `supervisor start`. After a reload the supervisor would
  dispatch through the new account while throttling on the old one's
  utilization. Step 1 used `claude --config-dir`, a flag `claude` does
  not have, and now uses `CLAUDE_CONFIG_DIR=<dir> claude /login`. The
  cheat sheet's `load_settings` snippet also gained the
  `from pathlib import Path` it needed to run.
- **The same defect in four more docs.** `docs/runbook.md` and
  `docs/first-time-setup.md` passed `--status` to `queue list`. The
  option belongs to `queue states`, where it is repeatable
  (`--status running --status failed`). A comma-joined value, which
  `docs/first-time-setup.md` also used, matches no status and prints
  nothing. ADR-0010 and ADR-0014 describe `effort list`, `config show`
  and `config validate` subcommands that were never built. Both ADRs are
  append-only, so each gets a dated update that says so and names what
  to use instead.
- **A docs-vs-CLI gate so that cannot recur.**
  `tests/unit/test_docs_cli_refs.py` walks the real typer command tree
  (`typer.main.get_command(app)`) for every `claude-task-runner ...`
  invocation in a code span or fenced block. It covers `docs/**/*.md`,
  `README.md`, `CHANGELOG.md` and the skills' `SKILL.md` files, plus
  every code span led by a top-level group, such as `sidecar answer`.
  It fails on an unknown command, subcommand or option, and it checks
  each option against the command it follows, as click does. The walker
  has known-answer tests, including the original bug. It is also
  enumerated over the whole tree, so it cannot reject a real command or
  option. Docs that name a command only to say it was never built are
  allowlisted per file with a reason. These are `config init`,
  ADR-0030's `why-blocked`, and the two ADRs above. A companion test
  fails when an allowlist entry goes stale.
- **Docstrings in the package gave CLI forms that fail.** The docstring
  of `usage/oauth_refresh.py` ran `usage refresh` with `--queue` and
  `--config` after the subcommand, which fails with
  `No such option: --queue`. `refresh` takes no options, nothing on the
  `usage` path takes `--queue`, and `--config` belongs to the `usage`
  group, so it has to come first:
  `claude-task-runner usage --config <queue>/claude_runner.toml refresh`.
  The `usage_cmd.py` docstring now says so too. `cli/install_skills_cmd.py`
  named an `uninstall-skills` command and `cli/install_cmd.py` a bare
  `uninstall`; the commands are `install-skills uninstall` and
  `install uninstall`. In `queue/schema.py`, the `note` of a `requires`
  element was said to be reported by `why-blocked`, a `queue` subcommand
  that was never built. Notes appear in `queue show`, in the task's
  `readiness hold:` deferred reason, and in a refused
  `queue force-dispatch`.
- **`supervisor drain --help` no longer says systemd restarts a drained
  supervisor.** It said the unit is `Restart=on-success`. The unit that
  `claude-task-runner install` writes is `Restart=on-failure` with
  `RestartPreventExitStatus=0`, so a supervisor that drains and exits 0
  stays down. The help now says to run
  `systemctl --user restart claude-task-runner` under systemd. It also
  says the unit's `ExecStop` is `supervisor drain --no-wait` only when
  `[supervisor].adopt_workers` is false. By default `ExecStop` is
  `supervisor stop`, and the new supervisor adopts the running workers.
- **Each CLI module docstring lists every subcommand of its group.**
  `supervisor_cmd.py` listed `start | stop | status` without `drain`.
  `usage_cmd.py` lacked `whoami` and `refresh`, `queue_cmd.py` lacked
  `template`, `install_skills_cmd.py` lacked `list`, and
  `watchdog_cmd.py` lacked `register` and `queues`.
- **The CLI gate now covers the package's scripts, Python sources and CLI
  module docstrings.** `tests/unit/test_docs_cli_refs.py` also walks
  every invocation in the shell scripts under `src/claude_task_runner`
  (the skills' helpers and `cron/watchdog.sh`), and in every string
  literal, docstring and f-string in `src/claude_task_runner/**/*.py`.
  A small lexer splits each script into code, which is checked word by
  word like a fenced block, and prose: comments, quoted strings and
  heredoc bodies. A heredoc fed to `python` is parsed as Python, so the
  argv list with which `fetch_all.sh` runs `sidecar show` is checked
  too. Prose is checked in code spans, and a bare `claude-task-runner`
  in prose counts only when the next word is a top-level group or an
  option. So English such as "claude-task-runner not on PATH" is not
  read as a command, while an echo telling the operator to run a
  missing subcommand still fails. A second test fails when a CLI
  module's docstring leaves out a subcommand of its group. The walker
  also no longer reads a placeholder followed by a path, such as
  `<queue>/claude_runner.toml`, as a `<` redirection. That misreading
  ended the walk early, so the words after the placeholder went
  unchecked. Run against the sources as they were before these fixes,
  the new tests fail on the `--queue` preflight, the `why-blocked` note
  and the five incomplete subcommand lists, and on nothing else. The
  `Restart=` claim and the bare `uninstall` are not invocations the gate
  can check. Once `claude-task-runner worktree` existed, the gate also
  caught two docstring spans in `worktree/reclaim.py` that named git's
  own worktree removal without the `git` prefix, so they read as a
  subcommand of that group. They now say `git worktree remove`.
- **Docs no longer advertise two config keys that make the config
  unloadable.** `docs/runbook.md` ("Sidecars piling up", step 3) told
  operators to set `[sidecar].unanswered_auto_recommended_s`, and
  `docs/architecture.md` ("Extension points") told them to set
  `[notify].channels`. Both tables had been deleted as dead config in the
  2026-06-13 audit, and every settings model is `extra="forbid"` — so an
  operator following either instruction got a `claude_runner.toml` that
  refused to load *entirely*, not merely an option that did nothing. Both
  paragraphs are removed. The auto-answer behaviour the runbook described
  was never implemented; sidecars are resolved by the operator via
  `/runner-answer-sidecar`.
- **A docs-vs-schema gate so that cannot recur** —
  `tests/unit/test_docs_config_refs.py` walks the `Settings` model for
  every valid config path, then scans `docs/**/*.md` for `[table].field`
  references, whole-code-span references, and the table headers and keys
  inside fenced `toml` blocks (the copy-paste path), failing on any that
  the schema does not define. `[throttle.*]` is allowlisted with its
  reason: ADR-0022 retired it, superseded ADRs 0015/0016 record it as
  history, and `config.loader._reject_legacy_throttle` already hard-errors
  on it — which is precisely why *its* docs stayed accurate while the two
  prose-only removals rotted.
- **Sidecar openness is accounted per QUESTION, not per response file
  (ADR-0031).** A request asking `q1`/`q2`/`q3` whose response answered only
  `q1` was reported *closed* by every openness test in the codebase —
  `list_open_sidecars` and, through it, the readiness gate, the
  orchestrator's eligibility sweep, the dispatcher's post-run status
  override, `sidecar list`, `fetch_all.sh` and `/runner-answer-sidecar` all
  asked only whether `response-NNN.json` existed. The unanswered questions
  became invisible to every counter at once and the task was released back
  to the runner as though the operator had decided. In the nlmixr2lib
  ingestion queue this hid **53 partially-answered requests**; the queue read
  as 73 open requests when it actually owed 166 answers across 124.
  A request is now open while any asked `questions[].id` is missing from the
  response's `answers[].id`; a request with no questions (a `file_and_exit`
  notification) is still closed by the presence of a response.
- A sidecar whose own JSON cannot be read well enough to tell what it asked
  is now reported **open with an `error`** rather than silently counted as
  answered. Openness reads `questions[].id` / `answers[].id` from raw JSON
  instead of validating the whole payload, so schema drift that says nothing
  about answeredness (a legacy request missing `created_at`, an answer
  carrying `notes`) no longer decides it.

### Added

- **`sidecar answer` refuses to write a partial response.** Omitting any
  asked question id exits 3 and names the missing ids — the durable half of
  the fix, since correcting the counts alone would let the gap keep being
  created. `--merge` carries forward the recorded answers for ids not
  supplied (so topping up an already-partial request stays one call), and
  `--allow-partial` is the explicit override, which leaves the sidecar open
  on the omitted ids.
- **`sidecar list` names the outstanding question ids.** JSON output gained
  `outstanding`, `answered`, `partial`, `response_path`, `prompts`,
  `proposed_names`, `n_open` and `n_outstanding_questions`; the human
  listing marks partial rows and ends with a request/question count. An
  operator told only which task is stuck cannot tell what is still missing.
  `fetch_all.sh` now presents only the outstanding questions.
- **`SidecarOption.proposed_names`** — canonical names an option would create
  or adopt, as structured data. Routine naming questions are mechanically
  triageable (collision-check against a register, auto-approve under a
  standing rule) only if a machine can tell which token is the name;
  recovering them from prose was tried on a real backlog and abandoned
  because it produced both false collisions and false all-clears. The
  `agent-stop-and-ask` skill now requires both this field and backticks
  around any proposed name in the option label.
- **`SidecarAnswer.notes`** — per-answer operator note. Answering flows
  already wrote it and `extra="forbid"` made those responses unreadable.

### Changed

- **`requires` (ADR-0030) is enforced on every dispatch path, and a held
  task now says so.** The selector already checked `requires` for every
  resume status — including an `awaiting_sidecar` task whose sidecars have
  all been answered — but nothing pinned that, and two paths bypassed the
  check outright: `force_dispatch.tick_consume` and
  `dispatch_synchronously` spawn a dispatch without consulting the selector.
  Force-dispatch overrides the *throttle*; an unmet `requires` says the
  input the run reads is not on disk, so forcing past it only buys a worker
  that re-discovers the gap and exits. Both force paths now refuse and name
  the missing element, and `_dispatch_one_safely` — the thread entrypoint
  every dispatch path funnels through — re-checks as a structural backstop
  (which also closes the selector's select-then-spawn race). A regression
  test enumerates every resume status against an unsatisfied requirement in
  both directions.
- A task the readiness gate holds is now parked as `deferred` with a
  `readiness hold: <reasons>` reason instead of being an invisible per-tick
  skip, so `queue list` shows why it has never run. No `next_eligible_at` is
  set — a cooldown would forfeit ADR-0030's promise to unblock the first
  tick after the element appears — and the park is written only on
  transition, leaves `attempts` / `runs` untouched, and clears itself back
  to `pending` once the requirement is satisfied. The marker scopes the
  self-healing: an operator's manual park and the pre-dispatch hook's exit-1
  deferral carry different reasons and are never cleared or overwritten.
- Default model for newly-authored tasks is now **Opus 5**
  (`claude-opus-5`), replacing `claude-opus-4-7` in `queue add`'s
  `--model` default, the `Task.model` schema default, and the
  `runner-add-task` skill. Opus 5 reaches the same result with fewer
  tokens. `claude-sonnet-5` replaces `claude-sonnet-4-6` in the
  documented model set; `claude-haiku-4-5` is unchanged (still current).
  Previous-generation models stay registered in `[effort_levels]` so
  queues with in-flight task YAMLs naming them keep dispatching rather
  than raising `UnknownModel`. Cold-start `[ema.priors]` for the new
  models are copied from their predecessors rather than lowered:
  over-estimating token spend only paces the dispatcher more
  conservatively, whereas under-estimating would over-dispatch into the
  weekly cap, and the EMA converges on the real figures after a few
  completions.

### Added

- **Session-affinity TTL — `[dispatch].affinity_ttl_seconds` (default 1.5h).**
  Session affinity (ADR-0024) pins a task to the account hosting its Claude
  session, because a session created under one `CLAUDE_CONFIG_DIR` cannot be
  resumed under another. But once the session has been idle past the TTL its
  resume/cache value is spent (prompt-cache warmth is gone after ~1h), so if
  the host account cannot take the task (throttled / paused / at-capacity) the
  orchestrator now clears the session — the automatic form of `queue
  restart-fresh` — and dispatches it fresh on any eligible account instead of
  leaving it stranded on a throttled host while another account sits idle.
  Affinity is still honoured while the host has capacity and for sessions
  younger than the TTL; the feature never resumes a session on the wrong
  account (it clears first, then dispatches fresh). Set `affinity_ttl_seconds`
  very large to restore strict always-affinity behaviour.

- **Mechanical readiness gates — `Task.requires` (ADR-0030).** A task can now
  declare no-AI, no-dispatch preconditions the supervisor's selector checks
  every tick: `requires: [{kind: "file", path: "<rel-or-abs>"}]` (the path
  must exist) or `{kind: "sidecar_response"}` (all the task's sidecars are
  answered). A task with any unmet requirement is kept OUT of the candidate
  set — never dispatched to discover the gap — and is admitted the first tick
  after every element is satisfied. This brings a *file* wait to parity with
  the *sidecar-response* wait (always selector-side): no wasted dispatch
  cycle, no in-flight-slot churn, and unblock within one poll interval
  instead of a `deferral_recheck_cooldown_s` (~15 min) lag. Evaluated by
  `runner.readiness.unmet_requirements` (pure `Path.exists()` + set lookup,
  safe to run for the whole pending pool each tick); extend `ReadinessKind`
  + one branch to add gate types. Additive + defaulted (`[]`) — existing task
  YAMLs load unchanged; a queue opts in by populating `requires` on its tasks.

- **Opt-in dispatch block-list — `[dispatch].dispatch_block_file`
  (ADR-0029).** A queue-relative JSONL of task ids the candidate selector
  skips outright when flagged `"block_dispatch": true` — *without*
  spawning a dispatch a pre-dispatch hook would only `exit 1` defer. Set
  it to e.g. `"needs_acquisition.jsonl"` so an operator's known-blocked
  parking (a paper awaiting a supplement/upstream) stops burning a
  dispatch+defer cycle every `deferral_recheck_cooldown_s` — which, on a
  low-`max_concurrency` account, briefly re-occupies the only slot each
  cooldown. Fail-safe: a missing file / malformed line / row without the
  flag means "not blocked", so a broken list never strands work. Unset
  (the default) disables the feature; queues without the convention are
  unaffected.
- **Sidecar re-file loop guard (ADR-0027).** A task that keeps filing
  sidecars without committing any progress now gives up to
  `failed_circuit_breaker` (stop_reason `sidecar_refile_loop`) after
  `failure_classifier.sidecar_refile_loop_threshold` (default 4)
  consecutive no-progress re-files, instead of looping
  `answered → re-dispatch → re-file the same blocker` forever. A run that
  commits resets the counter, so a legitimate ask→build→ask flow is never
  penalised. Adds `TaskState.sidecar_refile_count`. Complements the
  queue-side `block_dispatch` pre-dispatch hook check (which parks
  file/supplement/upstream blockers as `deferred`).
- **`agent-bash-patterns` worker skill — prevention half of the
  bash-poll-forever antipattern.** Companion to the reaper that
  *detects* `Rscript … &` + `until ! pgrep -f X; do sleep N; done`
  and kills with stop_reason `killed_bash_poll_antipattern`. The new
  skill teaches the dispatched agent not to write the loop in the
  first place: run long commands synchronously with `timeout`, via a
  marker-file sentinel, or in an `&&`-chain — never background-then-
  poll across two Bash tool calls. This guidance previously lived
  (wrongly) inside the nlmixr2lib `extract-literature-model` skill,
  which only reached one queue's workers; it belongs in the runner so
  every dispatched agent gets it. Incidents that motivated it:
  `frompeople-919/948/937/950` (2h–24h+ each).

### Fixed

- **Deferred tasks no longer leak their in-flight concurrency slot
  (ADR-0029).** `_reap_finished`'s subprocess-leak guard (ADR-0025) read
  `runs[-1].pid` to decide whether a finished dispatch thread left a live
  subprocess behind. A pre-dispatch `exit 1` deferral (ADR-0026) spawns
  no worker and appends no run, so `runs[-1]` stayed pointing at a *prior*
  real dispatch's pid — long exited, and often **recycled** by an
  unrelated process (or owned by another user, which `_pid_alive` reports
  alive on `EPERM`). The guard then mistook the recycled pid for a leaked
  subprocess and **held the slot forever.** On a `max_concurrency: 1`
  account this meant one `deferred` task pinned the only slot and the
  account dispatched **0% for days** (observed live 2026-07-08 on the
  `work` account: 614 runnable tasks starved behind 145 file-blocked
  deferrals; two parked tasks had `deferral_count` 858/860 with real
  prior `runs[-1].pid`s). `_recorded_subprocess_pid` now returns `None`
  when the task is `deferred` — a deferral has no subprocess to guard, so
  the slot frees on the next reap like any worker-less dispatch. The
  genuine-leak path is unchanged for statuses that do append a run.
- **Pre-dispatch hook `exit 1` deferrals no longer trip the circuit
  breaker — new parked `deferred` status (ADR-0026).** The hook's
  documented exit-code contract is `exit 1` = transient defer (an input
  awaiting operator re-acquisition or a pending trim), other non-zero =
  hard failure. The dispatcher ignored it and counted *every* non-zero
  hook exit toward `failure_circuit_breaker_threshold`, so a paper merely
  awaiting re-acquisition burned through the threshold and died as
  `failed_circuit_breaker` (observed live June 2026: `zotero-009` awaiting
  `PMID_22257150`, plus `zotero-015/074/081` and `frompeople-695` — their
  PDFs arrived later but the tasks never re-dispatched). `exit 1` now
  parks the task in a new `deferred` lifecycle status via
  `_record_pre_dispatch_deferral`: deliberately kept out of `runs` (so it
  never reaches the breaker counter) and out of the `attempts` count, and
  re-checked only after `[failure_classifier].deferral_recheck_cooldown_s`
  (default 15 min) instead of at every tick — preserving the
  anti-starvation property that made hook failures count in the first
  place. Other non-zero exits and hook timeouts remain hard failures and
  still reach the breaker. Adds three backward-compatible `TaskState`
  fields (`deferral_count`, `next_eligible_at`, `deferred_reason`);
  legacy state YAMLs load unchanged.

- **Worker-facing `agent-*` skills are now actually delivered.**
  `install-skills` shipped only the five operator `runner-*` skills;
  `agent-stop-and-ask` existed in the package but was in no install
  list, so the sidecar-protocol skill reached no dispatched worker
  (workers read `~/.claude/skills/`, populated by `install-skills`,
  since their prompt carries no skill injection). `SKILL_NAMES` is
  now `OPERATOR_SKILL_NAMES + AGENT_SKILL_NAMES`; `install-skills`,
  `uninstall`, `list`, and the doctor `skills_installed` check all
  cover both `agent-stop-and-ask` and the new `agent-bash-patterns`.
  `runner-merge-claude-branches` (previously installed by hand) is
  also added to the operator list so a clean `install-skills` and the
  doctor both account for it. First-time-setup docs now document the
  `install-skills` step and the worker-delivery model.

- **Dispatcher `_terminate` now verifies the parent actually exited
  and raises `TerminateFailed` on kill failures (2026-06-13 zombie
  post-mortem).** The audit-pass PG-wide signalling reaped MCP /
  shell grandchildren, but the dispatcher still trusted a successful
  `os.killpg(SIGKILL)` return as "the parent is dead" without
  verifying. Two failure modes were observed live: a `killpg(SIGKILL)`
  that raises `OSError` (the signal-send itself failed — EPERM, etc.)
  and a parent that survives SIGKILL (TASK_UNINTERRUPTIBLE on a hung
  syscall). In both cases the dispatcher previously returned
  cleanly, the run was finalized as `killed_by_cap`, the slot was
  freed, and the subprocess survived for hours afterward
  (`frompeople-903-farrell_2013` survived 30+ hours past the bogus
  kill, locking the `work` account's only slot). `_terminate` now
  resolves the pgid once, falls through to SIGKILL on a non-vanished
  SIGTERM OSError (logged at WARNING), and after the SIGKILL waits
  another 2 seconds for the kernel to reap the parent — raising
  `TerminateFailed` (ERROR-logged) when either step can't confirm
  death. The raise propagates out of `dispatch()` so the state YAML
  stays `"running"` with the recorded pid for the per-tick silent-
  orphan reaper, instead of clearing the pid on a still-alive
  subprocess. The integration test reproduces the live incident with
  a SIGTERM-ignoring shim.
- **Subprocess-leak follow-ups: adopted-path post-SIGKILL verify
  and a supervisor-side held-slot defence (zombie-consolidated).**
  Builds on the merged orphan-child fix (`start_new_session=True` +
  process-group signalling) and the three-layer heartbeat (PRs #57,
  #59). Two distinct paths the owned-path `_terminate` work above
  did not itself close:
  - The adopted-worker terminate (`_terminate_by_pid`) now polls its
    `alive` predicate for 2s after escalating to group SIGKILL. A
    worker in `TASK_UNINTERRUPTIBLE` (D-state) that doesn't reap
    surfaces an ERROR log naming the pid (the owned-path `_terminate`
    raises `TerminateFailed` in the same situation — see the entry
    above). Without this the cap-kill silently "succeeded" on a
    still-alive subprocess; the orchestrator would then free the slot
    and re-dispatch onto a busy account.
  - Supervisor-side post-kill PID sanity check
    (`runner.orchestrator._reap_finished`): after every dispatch
    thread exits, the orchestrator looks up the subprocess pid in
    the just-written run record and probes `os.kill(pid, 0)`. If the
    pid is still alive, the slot is **held** (not freed), a one-shot
    `subprocess_leak_detected` event + `critical`-level notification
    fire, and an ERROR log names the leak. Re-checks on subsequent
    ticks stay silent (deduped via
    `DispatchSlot.subprocess_leak_notified_at`) until the kernel
    finally releases the pid, at which point the slot frees normally
    and the queue can resume dispatching to that account. Defence in
    depth against any future code path that forgets to kill, or any
    kernel state the dispatcher's SIGKILL escalation can't break.

- **Audit remediation — bug-class findings (full-codebase triage,
  2026-06-13, branch `audit/full-codebase-2026-06`).**
  - **Dispatcher orphan-child leak:** the `claude --print` subprocess is now
    spawned with `start_new_session=True`, and cap/heartbeat terminations
    signal the whole process group (`os.killpg`, SIGTERM→SIGKILL), so MCP and
    tool grandchild processes no longer survive a cap kill. Signal-send and
    post-timeout `kill()` failures are now logged instead of silently
    swallowed, and a failed subprocess-PID persist escalates to ERROR with an
    `UNTRACKED-PID` marker (was a quiet WARNING).
  - **Corrupt-state re-dispatch:** an unparseable task state file is no longer
    treated as "not yet dispatched" — the orchestrator and force-dispatch
    paths log an error and skip it, so a completed task can't be re-dispatched.
  - **Watchdog `--config` now forwarded** to the supervisor that `watchdog
    tick` spawns, so the watchdog's policy and the live supervisor's policy
    can't silently diverge.
  - **Multi-account usage source no longer mutates caught exceptions** — a
    dedicated, type-preserving `MultiAccountSourceError` carries the account
    context while keeping the supervisor's exception-type routing intact.
  - Hardened error handling across the supervisor daemon, queue store, CLI
    commands and config loaders (narrowed broad `except` clauses, added
    missing log context).

### Removed

- **Dead configuration removed (audit 2026-06-13).** Deleted five settings
  sections with zero readers — `[notify]`, `[metrics]`, `[ui]`, `[sidecar]`,
  `[fixtures]` — plus the unread `[supervisor].sigterm_grace_s` and
  `[supervisor].dry_run` fields and the never-raised `QueueLayoutError`, so
  operators can no longer populate options that silently do nothing.

### Added

- **`RunRecord.pid`** — new optional field on each run record carrying
  the OS pid of the subprocess that run spawned. Survives dispatch
  finalization (unlike `TaskState.pid`, which is cleared on finalize)
  so the orchestrator's tick-level reap can probe `os.kill(pid, 0)`
  AFTER the dispatch thread exits. The check refuses to free the
  in-flight slot when the recorded pid is still alive — the
  supervisor-side leg of the subprocess-leak defence above. None on
  legacy run records, on pre-dispatch-hook failures (no subprocess
  spawned), and the field has a None default for backwards
  compatibility with state YAMLs written before this release.

- **Three-layer heartbeat: separate `dispatcher_alive_at` field and
  filesystem-activity verification for the silent-orphan reaper.**
  PR #57 wired `last_heartbeat_at` writes into the dispatch loop, but
  that field only ticks when the agent emits a stream-json event. A
  healthy run mid-Bash-subprocess (R package check, large download,
  OAuth refresh) can be silent for tens of minutes despite the
  supervisor and dispatcher being alive and well. The per-tick reaper
  would have flagged those tasks as SILENT and (once
  `heartbeat_silence_kill_s > 0`) SIGTERM'd them — wrongly. Two new
  layers protect against false positives without bogging down the
  reaper:

  1. **`dispatcher_alive_at` field on `TaskState`** plus a background
     monitor thread inside the dispatcher that writes this field every
     `[task_caps].dispatcher_alive_write_interval_s` (default 30s)
     regardless of stream-json events. The reaper's classifier
     consults BOTH fields: a fresh `dispatcher_alive_at` means the
     monitor thread is pumping the subprocess pipe, so the task is
     HEALTHY even when `last_heartbeat_at` is stale. The same baseline-
     correction trick used for `last_heartbeat_at` (treat values older
     than `last_started_at` as if from a prior attempt) is applied to
     `dispatcher_alive_at` so a stale prior-run write doesn't
     erroneously short-circuit the classifier. Legacy state YAMLs
     (pre-this-release) carry `dispatcher_alive_at = None` and fall
     back to the heartbeat-only path so an upgrade doesn't reap every
     running task.

  2. **One-shot bounded filesystem-activity walk** of the task's
     `working_dir` before acting on a SILENT/KILL verdict. When both
     heartbeat fields are stale, the reaper walks the worktree (depth-
     capped at 4, well-known noisy directories like `.git/`,
     `node_modules/`, `__pycache__/` skipped) for the most recent
     `st_mtime`. If anything was modified within
     `[task_caps].zombie_verify_fs_activity_window_s` (default 600s),
     the task is treated as HEALTHY and `last_heartbeat_at` is
     refreshed from the mtime so the next pass starts from a fresh
     baseline. The walk runs ONLY when the cheap signals already
     suggest a hang — at most once per in-flight task per reaper pass,
     gated by the Layer-2 short-circuit. Zero filesystem overhead when
     everything is healthy.

  Three new `[task_caps]` knobs (all with operator-friendly defaults
  so a no-config upgrade just works):

  - `dispatcher_alive_write_interval_s = 30.0`
  - `zombie_verify_fs_activity_window_s = 600.0`
  - (existing `heartbeat_persist_interval_s = 30.0` for the
    `last_heartbeat_at` rate limit, from PR #57)

  New tests: `tests/unit/test_dispatcher_alive_monitor.py` exercises
  the monitor thread (initial write, loop cadence, failure isolation,
  clock consultation), and `tests/unit/test_reap_silent_three_layer.py`
  covers the dual-heartbeat decision tree plus the filesystem
  verification step (recent mtime → HEALTHY-and-refresh, stale mtime
  → SILENT/KILL, missing Task YAML / no working_dir → skip FS check,
  FS function raises → skip FS check, Layer-2 short-circuit prevents
  the FS walk from running in the common HEALTHY case). An integration
  test in `tests/integration/test_dispatcher.py` asserts the field
  lands in the YAML during a normal dispatch.

- **Supervisor tick-failure outage detection (audit 2026-06-13).**
  Consecutive force-dispatch / reap / dispatch-tick failures are now counted;
  a sustained dispatch outage escalates to a prominent ERROR plus a
  `supervisor_dispatch_outage` event instead of the supervisor looking alive
  while never dispatching. Queue YAML loads are size-bounded to guard against
  pathological inputs hanging a tick. Adds regression coverage for the
  SIGTERM→SIGKILL escalation, corrupt-state skipping, drain-to-exit,
  all-accounts-exhausted, per-account reading isolation, state-machine
  IDLE/STOPPED invariants, and the silent-reaper TOCTOU race.

### Fixed

- **Steady-state silent-orphan reaper inside live supervisor.** The
  reaper added in PR #55 (`fix/reap-silent-orphans-on-restart`) ran
  exactly once at supervisor startup, on the assumption that the
  dispatcher's in-process kill-threshold check would handle the
  steady-state silent-but-alive case. That assumption broke on
  2026-06-12 with task
  `frompeople-680-yu_2017_acta_pharmacologica_sinica`: the dispatched
  `claude --print` subprocess (PID 4070819) stayed alive ~29 hours at
  0.8% CPU emitting zero stream-json events, holding the `work`
  account's only dispatch slot the entire time. The supervisor was
  alive and ticking; the per-dispatch silence check never fired
  because it is gated on event arrival (the dispatcher's
  `_dispatch_loop` blocks on `parse_lines(process.stdout)` and only
  re-evaluates the heartbeat threshold on a new event). SIGTERM on
  the recorded pid caused the subprocess to exit with `end_turn`
  cleanly — proving it was processing buffered work, not crashed —
  but its silence was invisible to every existing detection layer.

  Two-part fix:

  1. **Dispatcher persists `last_heartbeat_at` per stream-json event**,
     rate-limited to once per `[task_caps].heartbeat_persist_interval_s`
     (default 30s). Previously the timestamp was only written at
     dispatch finalization, so the YAML's heartbeat reflected a prior
     (finished) run for the entire duration of the current attempt.
     A live heartbeat in the YAML is what the per-tick reaper reads
     to distinguish healthy long-running tasks from silent ones; the
     rate limit (default 30s, alert default 300s) keeps a chatty
     subprocess from thrashing the filesystem.
  2. **Per-tick reaper in supervisor `daemon.run_forever`** runs every
     `[task_caps].steady_state_reap_interval_ticks` ticks (default 1
     — every tick) against the orchestrator's live in-flight slot map.
     Same SILENT/KILL semantics as the startup pass via a shared
     `_classify_and_act` helper; distinct stop_reasons
     (`silent_steady_state` vs `silent_on_restart`) and error
     prefixes (`silent-steady-state-reap` vs `orphaned-restart-reap`)
     so the audit trail separates restart-orphans from in-supervisor
     wedges. A TOCTOU re-check immediately before the demoting write
     prevents the per-tick pass from clobbering a concurrent
     dispatcher finalize. Skipped during drain mode so the operator's
     "finish what's running and exit" intent isn't subverted.

  New regression tests cover the dispatcher heartbeat-persist rate
  limit, the per-tick pass's SILENT/KILL/HEALTHY verdicts, the
  in-flight scope filter, the TOCTOU re-check, the two-tick
  progression from healthy to stale, the daemon's interval throttling,
  and the drain-mode skip.

- **`supervisor drain` now accepts `--config`**, so the systemd unit's
  `ExecStop=` line stops failing with `No such option: --config`.
  `cron/systemd_unit.py::_drain_command_from` generates the ExecStop
  argv by substituting `supervisor start` → `supervisor drain` on the
  ExecStart command — which left `--config /path/to/claude_runner.toml`
  attached. `drain` didn't declare a `--config` option, so every
  `systemctl restart` saw

  ```
  No such option: --config
  Try 'claude-task-runner supervisor drain --help' for help.
  ```

  in the journal and ExecStop exited `status=2/INVALIDARGUMENT`.
  systemd then fell through to its main SIGTERM kill which still
  triggered the supervisor's graceful-stop path, so end-to-end
  behaviour was correct — but the spurious failure made every restart
  look broken in logs (recurring since at least 2026-05-22). `drain`
  accepts `--config` as a no-op (it only signals the running supervisor
  via the queue's pidfile; settings aren't needed). New regression
  tests in `tests/unit/test_supervisor_cmd.py` lock the contract
  between the systemd-unit generator and the drain CLI by replaying the
  exact ExecStop argv the generator produces and asserting it parses.

### Changed

- **`/runner-status` reports per-account state from the v3
  supervisor snapshot.** The bundled `snapshot.sh` previously
  closed with a live `claude-task-runner usage render` block — one
  fresh `/usage` capture per call, which on multi-account queues
  showed only whichever account got picked, and burned tokens on
  every status check. It now reads the v3 `supervisor.json`'s
  `accounts` map directly and emits a per-account markdown table
  with state, 5h/weekly util, paused flag, per-account in-flight
  count (derived from the attributed `in_flight` records), 5h +
  weekly reset, scheduled wakeup, and last-capture timestamp. Drift
  messages — long, may contain pipes — render as a bulleted list
  below the table. Pre-v3 files (or v3 snapshots not yet ticked)
  soft-fail with an inline marker rather than aborting the script.
  The per-account snapshot is at most one `poll_interval_s` old
  (~30-60s on standard configs); operators who want a brand-new
  capture can still run `claude-task-runner usage render` directly.

### Fixed

- **Silent orphan reaper at supervisor startup.** When a supervisor
  exited ungracefully (OOM, SIGKILL, or a forced restart during a
  multi-day DNS outage observed 2026-06-05), the per-dispatch
  monitor threads that watched each subprocess's stream-json output
  died with the parent process — but the `claude --print`
  subprocesses survived, re-parented to init, with no monitor
  thread updating heartbeats or enforcing the kill threshold. The
  existing `reconcile_orphans` demoted every `running` state YAML
  to `failed` on the next supervisor start, but it did so
  undifferentiated: a task that had been silent for two days was
  auto-redispatched the same as one that was healthy when the
  supervisor died, frequently re-hanging on the original failure.
  A new startup pass `supervisor/reconcile_silent.py` runs BEFORE
  `reconcile_orphans` and grades each in-flight task by heartbeat
  freshness using the same `runner.heartbeat.evaluate` the
  dispatcher's monitor loop uses: SILENT tasks (alert window
  crossed, no kill threshold) flip to `possibly_hung` so the
  operator inspects rather than the orchestrator auto-redispatches;
  KILL tasks (kill threshold exceeded) flip to `failed` with
  `stop_reason="killed_by_silent_reaper"` and best-effort SIGTERM
  the recorded subprocess pid. The dispatcher now persists the
  subprocess pid into the TaskState YAML right after `Popen` (and
  clears it on finalization) so the reaper has a target to signal.
  HEALTHY tasks fall through to the existing `reconcile_orphans`
  demotion sweep for the normal session-resume recovery path.
- **CLI commands now auto-discover `<queue>/claude_runner.toml`.** Most
  CLI subcommands (`account list`, `account pause/resume`, `queue add`,
  `queue backfill-working-dir`, `queue force-dispatch`, `supervisor
  start`, `supervisor status`, `install`, `doctor`) accepted both
  `--queue` and `--config` but treated them independently — passing only
  `--queue` silently fell back to package defaults, hiding the operator's
  real `[[accounts]]` declarations and other queue-side overrides. Most
  visibly: `claude-task-runner account list --queue <q> --json` returned
  only a synthesised `"default"` placeholder while the live supervisor
  (which always passes `--config`) used the real `personal`/`work`
  accounts from `<q>/claude_runner.toml`. New helper
  `cli/_helpers.py::resolve_per_queue_config` applies the obvious
  resolution: explicit `--config` wins; otherwise pick up
  `<queue>/claude_runner.toml` if it exists; otherwise fall back to
  package defaults (matches the historical no-config behaviour). The
  `install` command additionally propagates the auto-discovered path
  into the installed systemd ExecStart so the daemon sees the same
  config the operator did.

### Added

- **`--add-dir` propagation for dispatched agents.** Claude Code's
  `--print` mode sandboxes each session to its cwd; reads/writes
  outside that scope are silently blocked. The dispatcher now always
  passes `--add-dir <queue_dir>` so the agent can reach sources under
  the queue (papers/, from_people/), the sidecar protocol, and the
  reports/ tree. New optional Task YAML field `additional_dirs:
  list[Path]` declares per-task extras (e.g. a sibling repo, a
  shared data tree); each entry is forwarded as another `--add-dir`.
  A new `claude-task-runner queue add --add-dir <dir>` flag
  (repeatable) sets the field at task-creation time. Backward
  compatible — existing task YAMLs that omit `additional_dirs` keep
  working and pick up the queue dir automatically.
- New `[dispatch].auto_detect_paths_in_prompt` setting (default
  `false`, opt-in). When enabled, the dispatcher extracts absolute
  paths from the task prompt, walks them to the containing
  directory, and adds the existing ones to the per-dispatch
  `--add-dir` list. Useful for queues whose prompts inline source
  paths; off by default to avoid false positives from prose-y prompts.
- The supervisor's per-task dispatch log gains an `add_dirs=[...]`
  field showing the resolved scope so operators can verify what each
  agent was actually granted (truncated past ~300 chars).
- New `runner/add_dirs.py` module owns the resolution logic; covered
  at 91% by `tests/unit/test_add_dirs.py`.
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
### Changed

- **The default worker model is `claude-opus-5-5`.** `Task.model`,
  `queue add --model` and the packaged `[effort_levels]` / `[ema.priors]`
  defaults now name Opus 5.5 (operator directive 2026-09-24). The
  `claude-opus-5` entries stay so queues with in-flight task YAMLs keep
  dispatching. Opus 5.5 needs Claude Code 2.1.280 or newer on the dispatch
  host; an older CLI fails every dispatch with
  `400 ... does not support this model`.
