# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
