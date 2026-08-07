# Changelog

Notable changes to `claude-task-runner`. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning is
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The project is pre-1.0 — minor versions can introduce breaking changes.
Breaking changes are called out in the version notes.

## [Unreleased]

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
