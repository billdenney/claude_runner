# claude_runner

[![CI](https://github.com/wdenney/claude_runner/actions/workflows/ci.yml/badge.svg)](https://github.com/wdenney/claude_runner/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

A token-budgeted to-do list runner for the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview).

`claude_runner` drains a directory of YAML task files through Claude, running
enough tasks in parallel to make full use of your 5-hour token window, while
holding weekly usage below a safe ceiling until the last day of the week. Each
task is resumable — interrupt the runner, restart it, and Claude picks up the
same session where it left off. When a task finishes, a Stop hook writes the
completion record and appends an event to `.claude_runner/events.ndjson`, so
supervising processes can react in real time.

## Why you'd use it

You have a list of roughly-similar Claude jobs (refactors, code reviews,
batch analyses) and you want to:

- **Not waste quota.** Pro/Max/Teams plans have a 5-hour rolling window that
  resets whether you used it or not. `claude_runner` adapts concurrency so at
  least 80 % of that window is used on every reset.
- **Not blow the weekly cap.** The runner keeps weekly spend under 90 %
  until the last day of the week, so one heavy Monday can't ruin Friday.
- **Survive interruptions.** Close your laptop, kill the process, run out of
  disk — next `run` resumes every in-flight task from its captured session id.
- **Know when tasks finish.** The Stop hook fires synchronously on each
  completion; the event stream is a real file you can `tail`.
- **Not bleed tokens on a broken queue.** A single failed task is logged and
  skipped; a burst of failures trips a circuit breaker and stops the run.

## Install

```sh
pip install claude-runner
# or, for the raw-API rate-limit regime:
pip install "claude-runner[api]"
```

You also need the `claude` CLI installed and authenticated (see the
[Claude Code docs](https://code.claude.com/)). If you want the default budget
source, install [`ccusage`](https://github.com/ryoppippi/ccusage).

## 60-second quickstart

```sh
# 1. Scaffold a todo directory with a sample task and a commented config
claude-runner init ~/my-claude-queue
cd ~/my-claude-queue

# 2. Add a task (only --dir is required; everything else has defaults)
claude-runner new "Summarize README.md" --dir todo/

# 3. Drain the queue
claude-runner run
```

`claude-runner status` shows the task table, token usage, and circuit-breaker
state.

## Task YAML schema

Only two fields are required: `prompt` and `working_dir`. A minimal task:

```yaml
# todo/001-summarize.yaml
prompt: Summarize README.md and write the result to summary.md.
working_dir: ~/projects/foo
```

The full set of fields and their defaults:

| Field                    | Required | Default                                   |
|--------------------------|----------|-------------------------------------------|
| `prompt`                 | yes      | —                                         |
| `working_dir`            | yes      | —                                         |
| `id`                     | no       | file stem (`001-summarize`)               |
| `title`                  | no       | first non-blank line of `prompt`, trimmed |
| `allowed_tools`          | no       | `config.default_allowed_tools`            |
| `model`                  | no       | `config.default_model`                    |
| `effort`                 | no       | `config.default_effort` (`medium`)        |
| `max_turns`              | no       | derived from `effort`                     |
| `estimated_input_tokens` | no       | derived from `effort`                     |
| `depends_on`             | no       | `[]`                                      |
| `priority`               | no       | `normal`                                  |

### Effort tiers

The `effort` field bundles extended-thinking budget and `max_turns` so you
don't have to reason about both separately:

| `effort` | thinking budget | `max_turns` | initial token estimate |
|----------|-----------------|-------------|------------------------|
| `off`    | 0               | 10          | 8 000                  |
| `low`    | 2 000           | 20          | 25 000                 |
| `medium` | 8 000           | 40          | 80 000                 |
| `high`   | 24 000          | 80          | 250 000                |

You can still override `max_turns` or `estimated_input_tokens` explicitly; an
explicit value wins over the tier default.

## Configuration (`claude_runner.toml`)

`claude-runner init` writes a commented config you can edit. The key fields:

```toml
regime = "pro_max"              # or "api"
plan = "max5"                   # pro | max5 | max20 | team | custom
budget_source = "ccusage"       # ccusage | context | api_headers | static
backend = "asyncio"             # asyncio | subprocess
max_concurrency = 8
min_utilization = 0.80          # ≥80% of the 5-hour window
weekly_guard = 0.90             # stay below 90% until the last day
max_consecutive_failures = 3
failure_rate_threshold = 0.5
failure_rolling_window = 10
discovery_cache_ttl_s = 60
default_effort = "medium"
default_model = "claude-opus-4-7"
```

Any field can also be set via the `CLAUDE_RUNNER_*` env var (e.g.
`CLAUDE_RUNNER_MAX_CONCURRENCY=4`).

## How scheduling works

1. The scheduler asks `TodoCatalog` for ready tasks (pulled from a cache that
   refreshes every ~60 s, so new YAML files show up automatically).
2. It asks the `TokenBudgetController` whether it's safe to start the next
   task — the controller knows current 5-hour and weekly usage from
   `ccusage` and the EMA cost of recent tasks.
3. If `OK`, a backend (asyncio by default) runs the task through the Agent
   SDK with `resume=<session_id>` if we have one. The first `SystemMessage`
   with `subtype=init` gives us a session id, which is persisted immediately
   so a `kill -9` is recoverable.
4. When the task finishes, the Stop hook records the outcome to
   `.claude_runner/state/<id>.yaml` and appends a `task_completed` event to
   `.claude_runner/events.ndjson`.

### Failure handling

A failed task is logged with its error, the circuit breaker increments, and
the next ready task is attempted. The breaker trips (and the run exits with
a non-zero status) when any of the following is true:

- 3 consecutive failures, **or**
- more than 50 % of the last 10 completions failed (with at least 4 samples).

User-initiated interrupts (Ctrl-C, SIGTERM) and budget-imposed pauses do not
count as failures.

## First run (live smoke)

```sh
claude-runner init ./smoke
cd smoke
claude-runner new "write hello to out.txt" --effort low
claude-runner new "write world to out2.txt" --effort low
claude-runner new "write !!! to out3.txt" --effort low
claude-runner run
claude-runner status
ccusage blocks
```

You should see three `task_completed` lines in
`.claude_runner/events.ndjson`, three state files with `status: completed`,
and `ccusage blocks` reflecting the aggregated spend.

## Decision log

Architectural decisions are recorded under
[`docs/decisions/`](docs/decisions/) — one numbered Markdown file per
decision. Start with the index in
[`docs/decisions/README.md`](docs/decisions/README.md) for the "why behind
the how."

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT — see [`LICENSE`](LICENSE).
