# Changelog

Notable changes to `claude-task-runner`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is pre-1.0 — minor versions can introduce breaking changes.
Breaking changes are called out in the version notes.

## [Unreleased]

### Fixed

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
