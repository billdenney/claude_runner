# ADR-0037: A silent worker meets its time caps on a tick, and a future mtime is no sign of life

- **Date:** 2026-10-30
- **Status:** accepted
- **Related:** ADR-0025 (restart-survivable workers, and the signs of life
  the reaper and adoption read)

## Context

The dispatch loop checked its caps only when a stream event arrived, so a
worker that wrote nothing was never stopped while its supervisor ran:

- `max_duration_s_per_task` was checked per event. A worker that hung
  before its first event, or in a call that writes nothing, was never
  capped. `ParkedWorkerTracker` caps parked workers only.
- `heartbeat_silence_kill_s` was checked just after the loop stamped the
  event's heartbeat, so the silence it measured was always about zero, and
  it could not fire.
- The silent-orphan reaper counts the monitor thread's `dispatcher_alive_at`
  tick as a sign of life, so a worker whose supervisor thread is alive is
  HEALTHY to it however long its agent is silent.

Separately, the reaper counted a file mtime in the future (a bogus touch,
a clock step) as fresh activity. In the filesystem walk it was the
maximum, so the check read the task as active and wrote a
`last_heartbeat_at` in the future, and the task was never reaped. A worker
log dated in the future kept its task HEALTHY the same way.

## Decision

1. The monitor thread's tick stays a sign of life for the reaper. A silent
   agent whose supervisor thread is alive is not marked `possibly_hung`:
   long tool calls, an R check or a large build, are silent for tens of
   minutes.
2. The caps only the clock can trip are checked on a tick. The dispatch
   loop's line sources yield a tick (`None`) after `_CAP_TICK_S` (10 s)
   with no line: the file tailer counts its polls, and on the pipe path a
   reader (`_PipeLines`) selects on stdout's descriptor and reads it
   itself. On a tick the loop checks the duration cap, on the time since
   `started_at`, and `heartbeat_silence_kill_s`, on the time since the last
   stream line of any kind. A cap it finds is recorded and the worker
   terminated as for a cap found on an event, and the run is
   `killed_by_cap` (`duration` for both, as before). A tick at which the
   wall clock reads earlier than the start or the last line checks
   nothing.
   - The check runs in the dispatch loop's own thread, inside its
     loop-error handling. A kill that fails there is handled as any loop
     error is: the stop is tried again, and a worker that still runs is
     parked.
   - A `tool_progress` line counts as output, though it is no event:
     claude writes one every 30 s while a tool runs, so a long tool call is
     not silence. A Bash poll loop stuck in such a call is the
     stuck-sleep-loop reaper's to stop.
   - The per-event silence check, which could not fire, is gone.
3. A file mtime more than a minute after now is no sign of life. The minute
   covers a file written while a reaper pass runs, which is dated after the
   `now` the pass took at its start. The reaper's walk leaves such a file
   out, as it does an mtime no datetime can hold, and the value an
   injected `fs_mtime_fn` returns is held to the same rule. A worker log
   dated so reads as no log write. A WARNING names the file once per file
   and mtime (`once.first_time`).

## Alternatives considered

- **A timer thread beside the loop**, the dispatcher-alive monitor or a new
  one. It would need its own route into the loop-error handling, and on
  the pipe path the loop's thread stays blocked in a read until the worker
  exits, so after a kill that failed the dispatch would wait on a worker
  that may never exit.
- **Marking a silent agent `possibly_hung` even with a live monitor**, at
  `heartbeat_silence_alert_s` or at the stuck-sleep-loop threshold. Long
  silent tool calls would be flagged, and the user chose not to.
- **Measuring silence from the last event**, leaving out `tool_progress`. A
  20-minute tool call would then be killed by any kill threshold shorter
  than that.
- **Clamping a future mtime to now.** A file dated years ahead would keep
  a hung task looking alive until then.

## Consequences

- A worker that writes nothing is stopped at its duration cap (4 h by
  default) within `_CAP_TICK_S`, owned or adopted, file-backed or on a
  pipe.
- `heartbeat_silence_kill_s` (0, off, by default) now works: a worker that
  writes no stream output for that long is stopped.
- On the pipe path, lines the reader has read and the loop has not taken
  are parsed with the rest of stdout after the loop.
- A file dated after now no longer keeps a hung worker looking alive.

## Reversibility

Low. The tick is a parameter of the line sources (`tick_s=None` turns it
off), and the future-mtime rule is one predicate, `_mtime_problem`.
