# Decision log

This directory records the architectural decisions that shape `claude_runner`
in the MADR (Markdown Architectural Decision Records) format. Each decision
is its own numbered file; the file's history is the authoritative record of
when and why a decision was made.

## Index

| #    | Title                                                              | Status   |
|------|--------------------------------------------------------------------|----------|
| 0000 | [Template](0000-template.md)                                       | —        |
| 0001 | [Default rate-limit regime is Claude Code Pro/Max/Teams](0001-default-regime-pro-max.md) | accepted |
| 0002 | [Use `ccusage` as the primary budget source](0002-ccusage-as-budget-source.md) | accepted |
| 0003 | [One YAML file per task; state stored separately](0003-one-yaml-per-task.md) | accepted |
| 0004 | [asyncio backend by default; subprocess backend as fallback](0004-asyncio-default-subprocess-fallback.md) | accepted |
| 0005 | [`effort` tier encoding (off/low/medium/high)](0005-effort-tier-encoding.md) | accepted |
| 0006 | [Circuit breaker halts runs on 3 consecutive or >50% rolling failures](0006-circuit-breaker-on-failures.md) | accepted |
| 0007 | [Stop hook writes completion state](0007-stop-hook-as-completion-signal.md) | accepted |
| 0008 | [Dynamic task discovery with ~60 s cache](0008-dynamic-discovery-cache.md) | accepted |
| 0009 | [Only `prompt` and `working_dir` are required in task YAML](0009-minimal-required-fields.md) | accepted |

## Workflow

When making a change that affects the public CLI, the YAML schema, the
budget algorithm, or the backend selection:

1. Copy `0000-template.md` to the next free number.
2. Fill in Context / Decision / Alternatives / Consequences.
3. Link it from the table above.
4. Commit the ADR in the same PR as the change it describes.

When replacing an older decision, mark the old ADR `superseded-by-NNNN` and
leave its body intact. Decisions are append-only — edits to a decision's
meaning must happen via a new numbered ADR so the project's history is never
silently rewritten.
