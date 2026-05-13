# claude-task-runner

Window-aware task runner for Claude Code.

Runs a queue of tasks against `claude` while proactively tracking the 5-hour
and 7-day usage windows. Throttles dispatch via EMA-driven concurrency,
schedules window-start wakeups via a long-running supervisor, and survives
reboots via a cron or systemd watchdog.

## Why this exists

Claude Code's rate-limit windows reset on a fixed cycle, but a naive runner has
no visibility into utilization until it crashes into the wall. This module:

- **Parses `claude /usage`** robustly with version-aware drift detection,
  so "is the parser broken?" is never silently confused with "is utilization
  zero?"
- **Throttles dispatch** in three bands (full / slowdown / stop) keyed off
  EMA-predicted post-dispatch utilization, all cutoffs configurable via TOML.
- **Schedules the next 5-hour window** automatically — the supervisor wakes
  up shortly after each reset and resumes draining the queue.
- **Resumes mid-task** via `claude --resume <session_id>` with automatic
  fallback to fresh restart on resume failure.
- **Survives crashes** via a cron-installed (or systemd `--user`) watchdog
  that restarts the supervisor with exponential backoff.

## Install

```sh
pip install -e '.[dev,ui]'
```

## Quick start

```sh
# in your queue directory — write a minimal claude_runner.toml first
# (see docs/first-time-setup.md for the template)
claude-task-runner queue add  # or `add-batch <manifest.csv>`
claude-task-runner install    # auto-detects systemd vs cron, asks confirmation
claude-task-runner supervisor start
```

For a step-by-step walkthrough from an empty directory, see
[`docs/first-time-setup.md`](docs/first-time-setup.md).

Interactive operation from inside Claude Code:

```
/runner-status
/runner-add-task
/runner-answer-sidecar
/runner-usage
```

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the component map and
data flow. See [`docs/decisions/`](docs/decisions/) for the rationale behind
each architectural choice.

## Status

Alpha. The plan is at
`/home/bill/.claude/plans/please-take-everything-learned-modular-crayon.md`.

## License

MIT
