# 0008. Dynamic task discovery with ~60 s cache

- Status: accepted
- Date: 2026-04-19
- Deciders: @wdenney

## Context

Users run `claude_runner run` for hours at a time and expect to be able
to drop new task YAML files into the todo directory while the runner is
working. A one-shot snapshot at startup would force them to restart the
process to pick up new work. At the same time, re-reading the entire
todo directory before every scheduling decision is wasteful when
nothing has changed.

## Decision

Task discovery goes through a `TodoCatalog` that re-scans the todo
directory at most once per `discovery_cache_ttl_s` (default 60 s).
Between scans, the scheduler queries `ready_tasks()` off an in-memory
snapshot. The catalog also exposes `invalidate()`, which the scheduler
calls after any state change that would alter readiness (a task
completes, fails, or becomes blocked), so correctness is never
dependent on the TTL expiring.

Unchanged files are detected by `(path, mtime_ns, size)` tuples and
reused without re-parsing.

## Alternatives considered

- **One snapshot at startup** — rejected: forces restarts to pick up
  new tasks.
- **Re-scan every iteration** — rejected: wastes syscalls when nothing
  changed, and re-parses unchanged YAMLs.
- **File-system watch (`inotify` / `watchdog`)** — rejected for v0.1:
  adds a platform-specific dependency for a problem that a 60 s TTL
  solves adequately. Worth revisiting if users want sub-second
  responsiveness.

## Consequences

- **Positive:** drop-new-YAML workflow works, startup is cheap, and
  state-change-driven invalidation keeps the scheduler from
  double-dispatching tasks that just completed.
- **Negative:** a user who drops a task in may wait up to 60 s before
  it's picked up. The TTL is exposed as a config knob for users with a
  different preference.
- **Follow-ups:** if operators ask for instant responsiveness, adding a
  `watchdog`-based catalog variant is a drop-in replacement since the
  scheduler only depends on the `TodoCatalog` interface.
