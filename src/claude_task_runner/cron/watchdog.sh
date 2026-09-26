#!/usr/bin/env bash
# Watchdog: invoked every minute by the crontab line that a cron
# `claude-task-runner install` adds. The systemd install does not use
# it; systemd restarts its unit itself.
#
# Delegates all logic to `claude-task-runner watchdog tick`, which:
#   1. Reads supervisor.pid for the one queue it manages, the last in
#      ~/.claude_task_runner/queues.json. A cron install and
#      `claude-task-runner watchdog register` replace that queue, and
#      `claude-task-runner watchdog unregister` removes it. One supervisor
#      runs per user, so any other queue listed there is ignored with a
#      WARNING line. A managed path that is not an existing directory is
#      skipped with an ERROR line.
#   2. If the supervisor is dead, no other process holds global.lock AND
#      backoff allows (cron.backoff.decide): restarts it via
#      `claude-task-runner supervisor start --queue ...`.
#   3. Logs to ~/.claude_task_runner/watchdog.log.
#
# Why a shell script and not direct cron invocation: ensures stdout/
# stderr go to a known log instead of cron mail; lets us probe `claude
# -task-runner` location through PATH adjustments (the cron environment
# is often minimal).

set -euo pipefail

LOG_DIR="${HOME}/.claude_task_runner"
LOG_FILE="${LOG_DIR}/watchdog.log"
mkdir -p "${LOG_DIR}"

# cron PATH is typically minimal; add common venv / pipx locations so
# we have a chance of finding the CLI without absolute paths in crontab.
PATH="${HOME}/.local/bin:${HOME}/.venv/bin:/usr/local/bin:${PATH}"
export PATH

if ! command -v claude-task-runner >/dev/null 2>&1; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) watchdog: claude-task-runner not found on PATH" >> "${LOG_FILE}"
  exit 1
fi

claude-task-runner watchdog tick >> "${LOG_FILE}" 2>&1
