# ADR-0019: Pre-init `.claude.json` before every claude spawn

- **Date:** 2026-05-18
- **Status:** accepted

## Context

The runner spawns `claude` two ways:

1. **Interactive `/usage` capture** via pexpect (`usage/capture.py`).
2. **Per-task `claude --print`** via `subprocess.Popen` (`runner/dispatcher.py`).

Both invocations open `<CLAUDE_CONFIG_DIR>/.claude.json` to decide
whether to show the first-run theme picker (`hasCompletedOnboarding`)
and the per-directory "Quick safety check" trust prompt
(`projects[<abs_dir>].hasTrustDialogAccepted`). When either gate is
unmet on an interactive spawn, the TUI sits at a prompt and the capture
times out at `TUI did not become ready within 30.0s`.

We hit this swapping a queue's `[claude].config_dir` from a mature
profile (`~/.claude_personal`, onboarding done, 310 trusted projects)
to a fresh one (`~/.claude`, work / Team account, no flags set,
empty `projects`). The capture failed; the supervisor sat in
`throttled_5h` on stale persisted utilization. The pexpect Phase 1
also couldn't dismiss either prompt cleanly — Claude Code v2.1.143
inserts `\x1b[1C` cursor escapes between every word in the prompt
text, so the legacy markers `b"Yes, I trust this folder"` and
`b"Quick safety check"` never match contiguously in the captured byte
stream, and interactive dismissal (`Enter`, `1\r`, arrow keys) does
not visibly advance the picker over pexpect (zero buffer growth on
every input we tried).

Manual edits to `.claude.json` unblocked the queue, but every fresh
queue / fresh account combination would need the same operator touch.
We want the runner to handle it.

## Decision

1. Add `claude_task_runner/claude_init.py` with
   `ensure_initialized(config_dir, trust_dir) -> bool`. The helper:
   - Resolves `config_dir` (empty/None → `~/.claude`) and `trust_dir`
     to a canonical absolute path.
   - Sets `hasCompletedOnboarding = True` if missing/False.
   - Sets `projects[<trust_dir>].hasTrustDialogAccepted = True`,
     creating the dict + entry as needed.
   - Writes atomically (`tmp` + `os.replace`) with mode `0o600`.
   - No-ops when nothing changed; no-ops if `.claude.json` doesn't
     yet exist (claude will create it on first launch and the next
     call will flip the flags).
2. Call it once just before each `claude` spawn:
   - `usage/capture.py::capture()` passes `Path.cwd()` as `trust_dir`
     (the supervisor's `WorkingDirectory=<queue>` propagates to the
     pexpect child's CWD, so this trusts the queue dir).
   - `runner/dispatcher.py::dispatch_task` passes `task.working_dir`
     (the per-task worktree).
3. Refactor `usage/capture.py` Phase 1 from a sequential
   trust-then-ready check into a `for` loop (≤ 5 iterations) racing
   trust prompts, the theme-picker single-word marker
   `b"colorblind-friendly"`, and the ready marker `b"shortcuts"`.
   Single-word markers survive ANSI cursor escapes between every
   word. This stays as defensive coverage in case the
   `ensure_initialized` write loses a race with concurrent claude
   writes, or for first-launch cases where `.claude.json` doesn't
   yet exist.

## Alternatives considered

- **Document an operator workaround in the README.** Rejected:
  every operator who points a queue at a fresh `.claude` directory
  has to discover this independently; the failure surfaces as an
  opaque `capture timeout` and supervisor stuck in `throttled_*` on
  stale utilization.
- **Interactive TUI dismissal in pexpect.** Tried six different
  input sequences (`\r`, `1\r`, `1`, `\n`, arrow-down + Enter, `j` +
  Enter); none caused the picker's PTY buffer to grow. The picker
  and trust dialog don't echo or redraw on input, so we can't tell
  whether the bytes are even reaching the program. Brittle and
  version-specific.
- **One-shot init at supervisor startup.** Rejected: misses fresh
  per-task `working_dir` trust state for dispatch, and the helper is
  cheap enough to call per-spawn that the extra coverage is worth
  the few microseconds.
- **Global `hasTrustDialogAccepted: true`.** Rejected: the comment
  block in `capture.py` advertised this in older claude versions,
  but Claude Code 2.1.143 ignores the top-level flag and only honors
  the per-directory entry inside `projects`. Confirmed by direct
  test (setting top-level `true` did not bypass the per-dir prompt).

## Consequences

- Operators no longer need to hand-edit `.claude.json` for a queue
  pointed at a fresh / never-used `config_dir`. First capture
  initializes; subsequent captures are a no-op.
- The pre-write races with concurrent writes by `claude` itself.
  We accept last-writer-wins because the flags are sticky (claude
  never unsets them once seen) and the race window is small (≤
  read+write of a 20–200 KB JSON).
- Phase 1 of `capture` now tolerates onboarding prompts arriving in
  any order, so future TUI versions with additional first-run
  screens degrade more gracefully than the previous strict-order
  check.
- The capture.py docstring's "operator escape hatch" note is now
  obsolete (the helper does it automatically) but the comment is
  kept as documentation of the underlying flags.

## Reversibility

Low. Removing the two call sites in `capture.py` /
`dispatcher.py` and deleting `claude_init.py` reverts the auto-init
behavior. The Phase 1 loop can stay or revert independently — it
has no dependency on the helper.
