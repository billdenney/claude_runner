# 0007. Stop hook writes completion state

- Status: accepted
- Date: 2026-04-19
- Deciders: @billdenney

## Context

We need an "active" signal when a task finishes so that its status file
can be updated and any supervising process can react. The Claude Agent
SDK fires a `Stop` hook at the end of each turn with the stop reason and
session id; the raw message stream also terminates. Either could be
used.

## Decision

Register a per-task `Stop` hook via `ClaudeAgentOptions.hooks`. The hook
updates the task's state file (status, stop reason, error), appends a
`task_completed` or `task_failed` event to
`.claude_runner/events.ndjson`, and writes the per-task NDJSON log.

The backend additionally reads the final `ResultMessage` for token
accounting, but decisions about "did this task succeed?" come from the
Stop hook, because it has the authoritative `stop_reason`.

## Alternatives considered

- **End-of-iterator as the completion signal** — rejected: we lose the
  explicit `stop_reason`, so distinguishing success from `max_turns`
  termination gets murkier.
- **External process watcher** — rejected: introduces IPC for something
  the SDK already gives us in-process.

## Consequences

- **Positive:** a `tail -f .claude_runner/events.ndjson` gives a live
  feed of completions. Supervising processes can react without needing
  to know anything about the Python API.
- **Negative:** if the SDK's Stop hook semantics change, the status
  transitions will need updating. We keep the hook small (update file +
  emit event) so the blast radius of such a change is contained.
- **Follow-ups:** when the user requests email/webhook notifications in
  a future release, the emitter is the natural place to hook them in —
  not the Stop hook itself.
