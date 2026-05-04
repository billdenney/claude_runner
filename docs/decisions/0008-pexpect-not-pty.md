# ADR-0008: Use pexpect, not raw pty + subprocess

- **Date:** 2026-05-03
- **Status:** accepted

## Context

`claude /usage` is a TUI command that requires interactive PTY semantics:
spawn the process, wait for the input prompt to be ready, send the slash
command, wait for the output to render, exit cleanly. Two implementation
choices:

- Raw `pty` + `subprocess` from the standard library
- `pexpect`, the de facto Python interactive automation library

## Decision

Use **pexpect**. The `read-until-pattern` loop, EOF detection, and
timeout-on-read are exactly what `pexpect` provides; reimplementing them
on raw `pty` is busywork that we'd test less rigorously than the upstream
library.

Capture the full PTY stream by setting `child.logfile_read = io.BytesIO()`
and tee'ing to a forensics file (`<queue>/.claude_task_runner/usage_captures/<ts>.cap`).

## Alternatives considered

- **Raw pty + subprocess:** rejected; reimplements pexpect's well-tested
  features.
- **`ptyprocess` (pexpect's lower layer):** more direct, but loses the
  pattern-matching API we need anyway.
- **`asyncio` subprocess with PTY:** would let us run usage capture in the
  supervisor's event loop, but `pexpect` has known issues with asyncio.
  Run the capture in a thread pool from asyncio code instead.

## Consequences

- (+) Less code, more thoroughly tested.
- (+) Native pattern-matching API for the trust-prompt / shortcuts /
  Resets sequence.
- (-) `pexpect` has occasional issues on macOS with newer Python versions.
  Mitigated by pinning a minimum version (`>=4.9`).

## Reversibility

Medium. Replacing `pexpect` with raw `pty` would require rewriting
`usage/capture.py`; nothing else depends on the choice.
