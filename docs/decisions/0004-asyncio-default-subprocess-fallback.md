# 0004. asyncio backend by default; subprocess backend as fallback

- Status: accepted
- Date: 2026-04-19
- Deciders: @billdenney

## Context

To hit 80% utilization of the 5-hour token window we need to run several
tasks in parallel. There are two practical ways to do that against the
Claude Agent SDK:

- **asyncio**: a single Python process, N concurrent `query()`
  coroutines, gated by a semaphore. Token accounting and budget
  decisions happen in one event loop.
- **Subprocess pool**: N concurrent `claude -p --output-format
  stream-json` children, one per task. Each child is isolated.

## Decision

The default backend is `asyncio`. A `subprocess` backend ships alongside
it and is selected with `backend = "subprocess"` in the config.

## Alternatives considered

- **Subprocess only** — rejected: startup overhead per task, harder to
  observe usage/results in real time, and coordinating N live JSON
  streams is more code than coordinating N coroutines.
- **Thread pool** — rejected: the SDK is async-first and its network
  calls release the GIL unevenly; asyncio is a better fit.

## Consequences

- **Positive:** the default path is fast, easy to reason about, and
  keeps the budget controller authoritative with direct visibility into
  every message.
- **Negative:** both backends must be maintained; the subprocess path
  duplicates some parsing logic. We offset this by keeping the scheduler
  and budget controller backend-agnostic — backends implement a small
  `RunnerBackend` protocol and are interchangeable.
- **Follow-ups:** if running multiple concurrent Agent SDK sessions on
  one OAuth credential turns out to be unstable in practice, users can
  flip to the subprocess backend without any other config changes.
