# ADR-0002: Long-running supervisor + cron/systemd watchdog

- **Date:** 2026-05-03
- **Status:** accepted

## Context

The runner needs to start the next 5-hour window automatically (within a
few minutes of reset) without an operator on the keyboard. Two architectural
shapes were viable:

1. A long-running daemon that polls usage and sleeps until the next reset.
2. A cron job that re-bootstraps the runner each window.

The runner also needs to survive crashes and machine reboots.

## Decision

Use **both**:

- A long-running Python supervisor process is the primary scheduler. It
  polls `/usage`, advances a state machine, and dispatches/throttles tasks.
- A cron entry (or `--user` systemd unit, auto-detected and preferred when
  available) acts as a watchdog: if the supervisor is dead, restart it.

The supervisor itself never installs the watchdog without operator
confirmation — `claude-task-runner install` shows the proposed crontab
line or systemd unit and asks before writing.

## Alternatives considered

- **Cron-only (self-spawning crontab):** rejected because state-machine logic
  is harder to test when each window starts a fresh process; persistent state
  must be loaded from disk and stale-checked every time.
- **Supervisor-only:** rejected because a single crash of the supervisor
  (or a reboot) would silently halt the queue with no recovery.

## Consequences

- (+) State-machine code stays in-process; trivially testable with `FakeClock`
  and `FakeUsageSource`.
- (+) Reboot recovery is automatic via the watchdog.
- (+) systemd's `Restart=on-failure` policy is strictly better than a 1-minute
  cron poll — preferred when available.
- (-) Two moving parts to install. Mitigated by a single `install` command
  that auto-detects the right one and asks for confirmation.

## Reversibility

Medium. Switching to one or the other later means removing the watchdog
install or stopping the supervisor. State files are independent of which
mechanism is in use.
