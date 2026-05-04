#!/usr/bin/env bash
# Watchdog: invoked every minute by cron (or via a systemd timer).
#
# Delegates all logic to `claude-task-runner watchdog tick`, which:
#   1. Reads supervisor.pid for each registered queue.
#   2. If supervisor is dead AND backoff allows (cron.backoff.decide):
#      restarts via `claude-task-runner supervisor start --queue ...`.
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
