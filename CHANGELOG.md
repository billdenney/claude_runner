# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`plan = "auto"` budget calibration.** Static plan presets (pro / max5 /
  max20 / team) were added when cache reads were a minor fraction of API
  billing. Modern Claude Code workflows are cache-heavy and routinely
  consume 50-150M tokens per 5-hour block on a Max5 subscription, which
  is ~50-75x the `max5` preset of 2M. Setting `plan = "auto"` in
  `claude_runner.toml` (or via env) now tells the `TokenBudgetController`
  to consult the configured `budget_source` for the operator's own
  historical usage and set both `budget_5h_tokens` and
  `budget_weekly_tokens` accordingly, floored at the `max5` preset and
  capped at 300M / 3B. The `ccusage` source grew
  `historical_block_totals()` and `historical_weekly_totals()` methods to
  expose that data. Calibration runs once at scheduler construction; the
  decision is logged at INFO level and surfaced in `claude-runner
  status` so the operator can see exactly which numbers are in effect.
  The policy uses `max(historical blocks) * 1.25` (and the same for
  completed weeks) as the ceiling, matching what `ccusage` itself does
  for its "assuming X token limit" inference. This replaces an initial
  `p90`-of-history policy that throttled the runner well below the real
  rate limit (on a Max5 account with historical peak 135M and real cap
  ~225M, p90 gave 100M — 45% of real; max x 1.25 gives 169M — 75% of
  real, and self-calibrates upward as new peaks accrue). The real
  subscription rate limit is not programmatically exposed — it only
  surfaces in the Claude Code app UI via HTTP rate-limit headers — so
  `max x growth` is the best retrospective signal available until
  Anthropic exposes the limit in their CLI.

### Fixed
- **Subprocess backend now trusts the stream-json ``{"type":"result"}``
  message over the process exit code.** Claude CLI v2 has been observed
  to return exit code ``1`` after otherwise-successful long sessions
  (~6-7M cache reads, 20-45 min wall-clock), evidently as a
  non-deterministic cleanup-path quirk. Before this fix, the runner
  classified those sessions as ``FAILED`` even though the agent had
  committed and pushed its branch — e.g. in one production batch the
  runner marked 4 of 6 tasks ``failed`` when in fact all 4 had
  successfully pushed ``add-<Author>_<Year>_<drug>`` branches to origin.
  The stream-json ``result`` message (which carries ``stop_reason``,
  ``subtype``, ``is_error``, ``api_error_status``) is now captured and
  treated as authoritative: if the session ended cleanly per
  stream-json, the task is marked ``completed`` regardless of the exit
  code, and a ``claude_cli_exit_code_mismatch`` event is emitted so the
  operator can see the underlying CLI quirk. Converse case handled too:
  a stream-json ``is_error=true`` overrides a spurious ``rc=0``. True
  crashes (no ``result`` line emitted) still fall back to exit-code
  reporting. Four regression tests in
  ``tests/test_subprocess_backend.py``.
- **Subprocess backend now pipes the prompt via stdin instead of passing
  it as an argv.** Previously the entire prompt text was the positional
  `prompt` argument to `claude -p`, which made it visible in
  `ps aux` / `/proc/<pid>/cmdline`. A real extraction task then ran
  `pkill -f "Xu_2019_sarilumab.Rmd"` to clean up a stuck `rmarkdown::render`
  subprocess, which also matched its own parent claude process (whose argv
  contained that filename from the task prompt) and SIGTERMed itself ~95%
  through the task, discarding most of the work. Piping via stdin removes
  the prompt from cmdline entirely. Regression guard in
  `tests/test_subprocess_backend.py::test_subprocess_backend_pipes_prompt_via_stdin_not_argv`.
- Preamble now warns the task agent explicitly against `pkill -f <pattern>`
  with patterns that could match its own context (filenames, task ids,
  keywords from the prompt). Points the agent at narrower alternatives
  (`pgrep -x <program>`, match on R-internal call names, read PIDs from
  `ps aux` first).

### Added
- **Sidecar interaction protocol** for stop-and-ask workflows. Tasks can now
  pause for human input by writing a JSON request file to
  `.claude_runner/sidecar/<task_id>/request-NNN.json` and exiting `end_turn`;
  the runner transitions the task to the new `AWAITING_INPUT` status, emits an
  `awaiting_input_snapshot` event every `reporting_interval_s` seconds
  (default 60, adaptive if the emit itself is slow, paginated at
  `report_max_per_tick` tasks per tick), and writes the full awaiting-input
  table to `.claude_runner/status_snapshot.json`. The operator answers with
  the new `claude-runner input <task_id>` subcommand, which validates the
  answers against the request's question IDs, atomically writes the
  matching `response-NNN.json`, and flips the task to `READY_TO_RESUME`.
  Resumed tasks are prioritized ahead of new `PENDING` tasks on the next
  scheduler tick, and the operator's answers + notes are prepended to the
  continuation prompt so the skill can pick up where it paused. New modules:
  `src/claude_runner/sidecar/{schema,store}.py`. The `CLAUDE_RUNNER_TASK_ID`
  and `CLAUDE_RUNNER_SIDECAR_DIR` environment variables are set on every
  dispatched task so the skill knows where to write.
- **Per-task git worktrees** via an optional `git_worktree:` block in the
  task YAML (`repo`, `branch_name`, `branch_from`, `root`). When present,
  the runner fetches from `origin`, creates the worktree idempotently on the
  requested branch, uses the worktree path as the task's `working_dir`, and
  tears it down on terminal success (the worktree is preserved across
  `AWAITING_INPUT` and on failure so the operator can inspect). New module:
  `src/claude_runner/git/worktree.py`. A `worktree_root` config key in
  `claude_runner.toml` (with `${task_id}` templating) sets the default
  parent directory.
- New config keys: `worktree_root`, `reporting_interval_s`,
  `report_max_per_tick`.
- `claude-runner input` subcommand with `--answers`, `--from-file`,
  `--notes`, and `--cancel` flags.
- Test suite additions: `tests/test_sidecar_store.py`,
  `tests/test_worktree.py`, `tests/test_cli_input.py`,
  `tests/test_preamble.py`, `tests/test_scheduler_dispatch.py`.
- **Auto-injected task preamble** that prepends to every task prompt a
  concise description of the runtime environment the task has access to:
  the `CLAUDE_RUNNER_TASK_ID` / `CLAUDE_RUNNER_SIDECAR_DIR` env vars, the
  sidecar stop-and-ask protocol (with the concrete request schema and
  resolved paths), the pre-created git worktree (when applicable), and
  the gh-read-only rule. Opt-out via `inject_preamble: false` on a task
  YAML or `inject_preamble = false` in `claude_runner.toml`. The goal is
  to keep skill prompts generic — they need not embed runner-specific
  instructions because the runner prepends them at dispatch.

### Fixed
- Subprocess backend now passes `--verbose` to the `claude` CLI. claude CLI 2.x
  rejects `--print --output-format=stream-json` without `--verbose` and exits
  immediately, which previously caused every subprocess-backend task to fail
  with no useful output. Added a regression test in
  `tests/test_subprocess_backend.py`.
- Subprocess backend now raises the asyncio stream reader limit to 16 MiB
  (was the default 64 KiB). A single stream-json message from the claude CLI
  — especially one containing a large tool output, long extended-thinking
  block, or verbose debug payload — could exceed 64 KiB and trigger
  `ValueError: Separator is found, but chunk is longer than limit` mid-stream,
  failing the whole task. Added a regression test.

## [0.1.0] — unreleased

### Added
- Initial release.
- `claude_runner init` / `claude_runner new` for scaffolding a todo directory and individual task YAML files with reasonable defaults.
- Dynamic task discovery with a ~60 s cache — tasks dropped into the todo directory are picked up without restarting the runner.
- YAML task schema with only `prompt` and `working_dir` required; every other field falls back to package or config defaults.
- `effort` tier (`off`/`low`/`medium`/`high`) bundling extended-thinking budget, `max_turns`, and the initial token estimate used to size concurrency.
- Token budget controller with a 5-hour rolling window and a 7-day weekly window; targets ≥80% utilization while holding weekly usage below 90% until the final day.
- `ccusage`-based budget source with `claude /context` and raw API header fallbacks.
- Asyncio-default / subprocess-fallback runner backends; per-task Claude session resumption after interruption.
- Stop hook that writes completion state and appends a `task_completed` event to `.claude_runner/events.ndjson`.
- Circuit breaker that halts the scheduler on 3 consecutive failures or on a >50% rolling failure rate (configurable) — one failing task never stops the queue.
- Decision log seeded with ADRs 0001–0007 under `docs/decisions/`.
