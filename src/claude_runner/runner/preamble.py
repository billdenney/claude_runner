"""Runner-provided preamble that is prepended to every task prompt.

The preamble tells the model about runtime facilities the task has access
to that it cannot discover from the prompt alone:

- the sidecar stop-and-ask protocol (when ``AskUserQuestion`` is blocked),
- the pre-created git worktree (when ``git_worktree`` is set),
- the read-only ``gh`` CLI convention (do not create PRs),
- the environment variables the runner sets
  (``CLAUDE_RUNNER_TASK_ID``, ``CLAUDE_RUNNER_SIDECAR_DIR``).

The preamble is opt-out: set ``inject_preamble: false`` on the task YAML,
or ``inject_preamble = false`` in ``claude_runner.toml``, to skip it.

All paths are resolved at dispatch time so the model sees concrete
absolute paths rather than placeholders.
"""

from __future__ import annotations

from pathlib import Path

from claude_runner.todo.schema import TaskSpec

_SIDECAR_SECTION = """\
## How to pause for human input (sidecar stop-and-ask)

`AskUserQuestion` is NOT available in this environment. When you need a
decision from the operator, use the sidecar-file protocol instead:

1. Pick the next unused sequence number `N` (start at 1, increment if
   `request-001.json` already exists in the sidecar dir, and so on).
   Pad to three digits: `001`, `002`, ...
2. Write `{sidecar_dir}/request-NNN.json` with this schema:

   ```json
   {{
     "schema_version": 1,
     "task_id": "{task_id}",
     "sequence": 1,
     "created_at": "<ISO-8601 UTC>",
     "summary": "One-line description shown in the operator's snapshot",
     "context": "Optional longer free-text preamble the operator reads before answering.",
     "questions": [
       {{
         "id": "<stable question id>",
         "prompt": "<the question>",
         "options": [
           {{"value": "A", "label": "Option A (Recommended)"}},
           {{"value": "B", "label": "Option B"}}
         ],
         "multi_select": false,
         "recommended": "A"
       }}
     ],
     "state": "open"
   }}
   ```

   Rules:
   - `task_id` must equal `{task_id}`.
   - `sequence` must equal the `NNN` in the filename.
   - Each `questions[].id` must be unique within the request.
   - Each `questions[].options[].value` must be unique within a question.
   - If you provide `recommended`, it must match one of the option values.
   - `state` is always `"open"` in your initial write.
   - Write atomically (tempfile + rename) when possible.

3. **Immediately end your turn.** Return one short final message for the
   log (e.g. "Wrote request-001.json; awaiting operator answer.") and do
   not call any more tools. The runner will transition your task to
   `AWAITING_INPUT`.

4. When the operator answers with `claude-runner input`, the runner will
   resume you with a follow-up user message containing the answers. Use
   them directly and continue the task.

You can pack several related questions into one request to minimize
round-trips. Only use the sidecar for BLOCKING decisions that require a
human answer — log output and status updates belong in your final
message, not in a sidecar request.
"""

_WORKTREE_SECTION = """\
## Git worktree already set up

Your working directory (`{worktree_path}`) is a fresh git worktree the
runner created for this task, checked out on branch `{branch_name}`
from `{branch_from}`. Do NOT run `git worktree add`, `git checkout -b`,
or `git fetch origin` yourself. Verify with:

```bash
git branch --show-current    # expect: {branch_name}
git status -s                # expect: empty
```

If either looks wrong, stop and ask (via the sidecar protocol above) —
do not try to recover in-place. When done with your changes, commit and
push normally (`git push -u origin {branch_name}`). The runner will tear
down the worktree after the task reaches a terminal state; do NOT run
`git worktree remove` yourself.
"""

_PKILL_SECTION = """\
## Do not kill processes by command-line pattern

Never run `pkill -f <pattern>`, `pgrep -f`, `killall`, or any other command
that matches processes by command-line content against a pattern that
appears anywhere in your own context. Your parent `claude` process's
environment includes this prompt and every filename / keyword in it; a
pattern like `Xu_2019.Rmd` or `my-task-id` can match YOUR OWN process
and SIGTERM the agent mid-task, losing ~90 % completed work.

If you need to kill a child process you spawned (e.g. a stuck `Rscript
rmarkdown::render`), instead:

1. Capture the PID when you start it: `Rscript -e '...' & echo $!`, or
2. Look it up narrowly by program name: `pgrep -x Rscript` (exact-match,
   not `-f`), or
3. Match on an argument that is guaranteed NOT in your prompt:
   `pgrep -f 'rmarkdown::render'` (the R-internal call, not the filename).

When in doubt: `ps aux | grep <program-name>`, read the PIDs yourself,
then `kill <pid>`. Never use `pkill -f` with filenames or task
identifiers.
"""

_GH_READONLY_SECTION = """\
## GitHub CLI is read-only

The operator's `gh` authentication token is read-only. Do NOT run any
`gh` command that writes state — no `gh pr create`, `gh pr edit`,
`gh pr merge`, `gh issue create`, `gh issue comment`, `gh release create`,
or `gh api` with a non-GET method. Read-only commands
(`gh pr view`, `gh pr list`, `gh issue view`, `gh api` with GET) are fine.

When a PR or issue needs to be opened, push your branch and print the
suggested title and body at the end of your run; the operator will open
the PR manually.
"""

_HEADER = """\
# Runner environment

This task is running under `claude_runner`. The runner sets these
environment variables on your process:

- `CLAUDE_RUNNER_TASK_ID={task_id}` — identifies your task uniquely.
- `CLAUDE_RUNNER_SIDECAR_DIR={sidecar_dir}` — directory for stop-and-ask
  JSON files (see below).

The sections below describe facilities the runner provides. Follow them
exactly; they override any conflicting guidance inside the task prompt
or invoked skills.
"""


def build_preamble(
    *,
    spec: TaskSpec,
    sidecar_dir: Path | None,
    worktree_path: Path | None,
) -> str:
    """Render the preamble for a specific task.

    ``sidecar_dir`` should be the resolved absolute path for this task's
    sidecar directory (the runner creates the dir before dispatch).
    ``worktree_path`` is the resolved worktree directory when the task
    has a ``git_worktree`` block, else ``None``.
    """
    parts: list[str] = []
    parts.append(
        _HEADER.format(
            task_id=spec.id,
            sidecar_dir=str(sidecar_dir) if sidecar_dir else "(unset)",
        )
    )
    if sidecar_dir is not None:
        parts.append(
            _SIDECAR_SECTION.format(
                task_id=spec.id,
                sidecar_dir=str(sidecar_dir),
            )
        )
    if worktree_path is not None and spec.git_worktree is not None:
        parts.append(
            _WORKTREE_SECTION.format(
                worktree_path=str(worktree_path),
                branch_name=spec.git_worktree.branch_name,
                branch_from=spec.git_worktree.branch_from,
            )
        )
    parts.append(_PKILL_SECTION)
    parts.append(_GH_READONLY_SECTION)
    parts.append("---\n\n# Task prompt\n")
    return "\n".join(parts)


def should_inject(*, spec: TaskSpec, settings_inject: bool) -> bool:
    """Resolve whether to inject for this spec.

    Task YAML override wins; otherwise fall back to Settings default.
    """
    if spec.inject_preamble is not None:
        return spec.inject_preamble
    return settings_inject
