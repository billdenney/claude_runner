# Changelog

Notable changes to `claude-task-runner`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is pre-1.0 — minor versions can introduce breaking changes.
Breaking changes are called out in the version notes.

## [Unreleased]

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
