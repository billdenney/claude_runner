# ADR-0003: Pip-installable sibling repo location

- **Date:** 2026-05-03
- **Status:** accepted

## Context

The existing runner lives inside `mab_human_consensus/claude_runner_queue/`
but is intended to be reused across projects (see comments in
`queue-tend-helper.sh` referencing "other projects sharing this runner").

## Decision

The new module lives at `/home/bill/github/claude_task_runner/` as a
standalone repo, pip-installable via `pip install -e .` (or eventually
PyPI). Each project has its own per-queue directory and its own
`claude_runner.toml` overriding package defaults.

## Alternatives considered

- **Inside `mab_human_consensus`:** rejected because reuse across projects
  would require either copying the code or a complex import path.
- **`~/.local/share/claude-task-runner`:** rejected because pip-install
  patterns are more standard for Python tooling and integrate with
  pyproject.toml workflows (uv, hatch, etc.).

## Consequences

- (+) Reusable across projects with per-queue config.
- (+) Standard Python packaging — installable, testable, publishable.
- (-) The queue directory is decoupled from the runner code, so operators
  must know which version of the runner is installed.

## Reversibility

High. The queue's on-disk layout is independent of the runner's code
location; relocating the package is a `pip install` away.
