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

Then install the skills into `~/.claude/skills/`:

```sh
claude-task-runner install-skills --yes
```

This installs two kinds of skill (symlinked by default, so edits to the
source tree flow through):

- **Operator skills** (`runner-status`, `runner-usage`, `runner-add-task`,
  `runner-answer-sidecar`, `runner-merge-claude-branches`) — invoked by you
  from an interactive `claude` session.
- **Agent skills** (`agent-stop-and-ask`, `agent-bash-patterns`) — consulted
  by the *dispatched worker*, not by you. A worker's prompt is just
  `task.prompt` with no skill injection, so a worker discovers skills the
  same way any `claude` session does: from `~/.claude/skills/`. Because the
  worker runs as the same Linux user, installing here is what lets it load
  these. They no-op in interactive use, so installing them globally is
  harmless.

> **Custom `config_dir` caveat.** `install-skills` writes to
> `~/.claude/skills/`. If a queue sets `[claude] config_dir` to a non-default
> `CLAUDE_CONFIG_DIR` (e.g. `~/.claude_personal`), its dispatched workers
> read skills from *that* dir's `skills/` — mirror the skills there too
> (symlink or `--copy`) or the agent skills won't reach those workers.

`claude-task-runner doctor` reports any skills that aren't installed.

## 2. Create the queue directory

```sh
mkdir -p /path/to/my_queue/todo
cd /path/to/my_queue
```

This directory becomes the queue's root. Tasks live under `todo/<id>.yaml`;
state lives under `.claude_task_runner/` (auto-created on first dispatch).
Create the directory first: `queue add`, `queue force-dispatch`,
`supervisor start` and `install` exit 2 with
`--queue is not an existing directory` rather than create a queue from a
mistyped or deleted path.

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
# config_dir = ""   # CLAUDE_CONFIG_DIR override. Empty (default) = ~/.claude.
                    # Set to e.g. "/home/bill/.claude_personal" if `claude
                    # /login` for the dispatching account was run under a
                    # non-default config dir. The runner's `/usage` capture
                    # also reads from this dir, so the throttle math is
                    # measured against the correct account's quota.

[concurrency]
max_concurrency     = 2
initial_concurrency = 1   # cap until a first task completes in this queue

[hooks]
# Pre-dispatch: create the worktree (or any other per-task setup).
# Env vars exposed: $TASK_ID, $TASK_WORKING_DIR, $TASK_MODEL, $ATTEMPT, $SESSION_ID.
# Leave blank if your tasks don't need a worktree.
pre_dispatch_command  = ""
pre_dispatch_timeout_s = 120
post_dispatch_command  = ""
post_dispatch_timeout_s = 60
```

That's enough. The 5h and weekly throttle thresholds (`[dispatch_pct.*]`)
and every other setting come from the defaults. There is no plan setting:
the throttle works from the utilization percentages `claude /usage`
reports, which are already relative to the account's tier.

If your pre-dispatch hook creates a git worktree per task, the runner will not
remove those worktrees on its own. `claude-task-runner worktree reclaim`
removes the ones whose task is `completed` and already merged. To have the
supervisor do that periodically, add a `[worktree_reclaim]` block. Set
`periodic = true`, and point `lock_file` at the flock your hook takes. See
ADR-0034 and the "Task worktrees filling the disk" section of the runbook.

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

### Sandbox scope (--add-dir)

Claude Code sandboxes each dispatched session to its cwd; any
`Read`/`Write`/`Bash` against a path outside that scope is blocked.
The runner widens the scope automatically:

* The queue directory is always passed via `--add-dir <queue>` so
  the agent can reach sources under the queue (papers/, from_people/),
  the sidecar protocol, and the reports/ tree.
* Per-task extras can be declared via `--add-dir <dir>` on
  `queue add` (repeatable). They persist to the task YAML's
  `additional_dirs` list and are merged into each dispatch.

```sh
claude-task-runner queue add \
    --id 007-foo --title "..." --prompt-file /tmp/foo.txt \
    --add-dir /data/cohort-X --add-dir /home/bill/sibling-repo
```

Existing queues need no migration: the queue dir was always reachable
implicitly before; this change just surfaces the same scope through
the CLI and adds the explicit per-task knob.

## 5. Install the watchdog

```sh
claude-task-runner install
```

Run it from the queue directory, or pass `--queue <path>`. It auto-detects
systemd-user vs cron and asks for confirmation before writing anything.

- **systemd:** writes a `--user` unit that runs `supervisor start` for this
  queue, starts it now, and restarts it after a crash. A clean exit
  (`supervisor stop`, or a `drain` once in-flight tasks finish) leaves it
  stopped.
- **cron:** adds a crontab line that runs `claude-task-runner watchdog tick`
  every minute, and registers this queue in
  `~/.claude_task_runner/queues.json`. A tick restarts the supervisor of each
  registered queue that is not running, however it stopped, and backs off
  exponentially after repeated crashes (ADR-0002). A tick manages only the
  registered queues, which `claude-task-runner watchdog queues` lists.

## 6. Start the supervisor

With a watchdog installed there is nothing to run. systemd started the
supervisor in step 5, and cron's next tick starts it within a minute.
Check it with:

```sh
claude-task-runner supervisor status
```

If a cron watchdog has not started it after a minute,
`~/.claude_task_runner/watchdog.log` says why.

Without a watchdog, start the supervisor yourself:

```sh
claude-task-runner supervisor start
```

This runs in the foreground and logs to stderr. Only one supervisor runs
per user, so once a watchdog has started one, this command fails with
`another supervisor is already running`.

## 7. Watch the queue drain

```
/runner-status        # in-Claude skill — preferred
```

…or directly:

```sh
claude-task-runner queue states --status running --status completed --status failed
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
