# First-time queue setup

Walks an operator from an empty directory to a running supervisor draining
its first task. Replaces the absent `claude-task-runner config init`
subcommand referenced in older docs (see ADR-0017).

## 1. Install the package

```sh
pip install -e '/path/to/claude_task_runner[dev,ui]'
which claude-task-runner   # verify on PATH
```

The `dev` extras include `pytest` and the doctor's check dependencies; the
`ui` extras include the optional terminal UI.

## 2. Create the queue directory

```sh
mkdir -p /path/to/my_queue/todo
cd /path/to/my_queue
```

This directory becomes the queue's root. Tasks live under `todo/<id>.yaml`;
state lives under `.claude_task_runner/` (auto-created on first dispatch).

## 3. Write a minimal `claude_runner.toml`

A queue inherits every default from
`src/claude_task_runner/config/defaults/settings.toml`. The TOML at
`./claude_runner.toml` only needs to **override** what's queue-specific.
Settings are merged section-by-section; the schema is strict
(`extra="forbid"`), so any typo is flagged at load time.

A working minimum:

```toml
# claude_runner.toml — minimal queue config

[claude]
plan = "max20x"     # or "max5x" | "pro" | "team_standard" | "team_premium"
# config_dir = ""   # set if running under a non-default ~/.claude profile

[concurrency]
max_concurrency     = 2
initial_concurrency = 1   # cap until the EMA has warmed up

[hooks]
# Pre-dispatch: create the worktree (or any other per-task setup).
# Env vars exposed: $TASK_ID, $TASK_WORKING_DIR, $TASK_MODEL, $ATTEMPT, $SESSION_ID.
# Leave blank if your tasks don't need a worktree.
pre_dispatch_command  = ""
pre_dispatch_timeout_s = 120
post_dispatch_command  = ""
post_dispatch_timeout_s = 60
```

That's enough. Plan-derived 5h/weekly throttle thresholds and EMA priors
come from the defaults.

## 4. Add a task

The skill way (recommended — picks effort and tools interactively):

```
/runner-add-task
```

Or the CLI:

```sh
claude-task-runner queue add
```

Either path writes a YAML to `todo/<id>.yaml`. Inspect with:

```sh
claude-task-runner queue list
```

A first task's YAML is small:

```yaml
schema_version: 2
id: 001-hello-world
title: Smoke-test the runner
model: claude-haiku-4-5
effort: low
priority: normal
working_dir: /tmp/scratch   # optional; runs in cwd if omitted
allowed_tools: [Read, Bash]
prompt: |
  Print "hello" and exit.
```

## 5. Install the watchdog

```sh
claude-task-runner install
```

Auto-detects systemd-user vs cron; prompts for confirmation. After
install, the watchdog kicks the supervisor on boot and restarts it after
a crash (exponential backoff per ADR-0002).

## 6. Start the supervisor

```sh
claude-task-runner supervisor start
```

This is foreground by default — the supervisor logs to stderr. To run
detached, use the watchdog (already installed in step 5) and check
liveness with `claude-task-runner supervisor status`.

## 7. Watch the queue drain

```
/runner-status        # in-Claude skill — preferred
```

…or directly:

```sh
claude-task-runner queue list --status running,completed,failed
```

When the first task is `completed`, the setup is done.

## Health check anytime

```sh
claude-task-runner doctor
```

Runs the self-diagnostic battery — verifies that `claude` is on PATH,
the `claude /usage` parser still works against the current CLI version,
the supervisor lock is consistent, and the queue YAMLs parse.

## What to do next

* Read `docs/runbook.md` for the recipes that handle common
  oncall situations (drift detection, hung tasks, etc.).
* Read `docs/architecture.md` for the component map.
* Read `docs/decisions/` if you want to know why a piece of the runner
  works the way it does.
