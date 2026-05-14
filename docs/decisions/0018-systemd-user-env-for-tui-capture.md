# ADR-0018: Inject TERM and PATH into the generated systemd-user unit

- **Date:** 2026-05-14
- **Status:** accepted

## Context

`systemctl --user` units inherit the user-session DBus and the basic
session env (HOME, USER, XDG_RUNTIME_DIR), but they do NOT inherit the
operator's interactive shell environment. In particular:

- `TERM` is unset.
- `PATH` is the systemd default
  (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`),
  which does NOT include `~/.local/bin`.

Both of these silently broke `usage/capture.py` against the live Claude
TUI (binary v2.1.141), in two ways the state machine cannot recover
from on its own:

1. **No TERM** — pexpect spawns `claude` against a pty but the child
   can't negotiate terminal capabilities without TERM. The welcome
   banner renders garbled and never emits the `shortcuts` marker the
   capture loop waits for. Result: `UsageCaptureTimeout` every tick.

2. **`~/.local/bin` not on PATH** — for pipx installs, the `claude`
   binary lives at `~/.local/bin/claude`. The capture function calls
   `shutil.which(claude_executable)` (default `"claude"`) at the top
   of `capture()`; with the systemd-default PATH that returns None
   and the function raises `UsageCaptureSpawnError`.

In both cases the state machine treats the result as a tick-level
error (per ADR-0012's failure-class routing) and leaves the snapshot
unchanged. The supervisor sits in initial IDLE forever, the queue has
work, dispatch never starts. Observed live during a fresh queue
bring-up on 2026-05-14: 1582 pending tasks, supervisor IDLE for
30+ minutes, no dispatches.

The pipx install correctly produces an absolute ExecStart for
`claude-task-runner` itself, so the supervisor binary resolves; but
`claude` is invoked by name from `capture()` and falls back to PATH
lookup.

## Decision

The systemd-user unit template (`cron/systemd_unit.py:build_unit_text`)
unconditionally injects two `Environment=` lines:

```
Environment=TERM=xterm-256color
Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

`%h` expands per-user, so the unit works for any operator. Operators
with non-standard layouts (e.g. pyenv-based Python with `claude` under
`~/.pyenv/shims/`) can override via
`systemctl --user edit claude-task-runner.service`.

A regression test in `tests/unit/test_systemd_unit.py` asserts both
lines are present in the generated unit text.

## Alternatives considered

- **Resolve `claude` absolutely at install time** and bake the path
  into the per-user TOML. Rejected: makes the config non-portable
  across users / machines and ties install state to a specific Claude
  binary version. The PATH approach lets pipx upgrades stay seamless.
- **Set `TERM` lazily inside `capture()`** before calling
  `pexpect.spawn(env=...)`. Rejected: would need a defensible default
  (which we pick anyway as `xterm-256color`); also doesn't address the
  PATH issue, which fires at `shutil.which()` time before pexpect is
  even invoked.
- **Document the env vars in `first-time-setup.md` and ask the
  operator to add them**. Rejected: this bites every new install and
  the symptom (supervisor stuck in IDLE with empty drift message) is
  not self-explanatory. Worth defaulting in.

## Consequences

- New installs work out of the box for the common pipx layout.
- The unit text is slightly more opinionated about the operator's
  environment, but operators who need a different PATH/TERM can edit
  the unit; the change does not block them.
- The regression test pins both env vars in place — future cleanup
  passes won't accidentally drop them.
- Pre-existing units installed before this change carry the old
  template. The fix only takes effect after `install uninstall &&
  install --queue ...` or a manual `systemctl --user edit`. Operators
  with running queues should run the reinstall sequence as a one-off.

## Reversibility

Low risk. Removing the `Environment=` lines reverts to the old
template; existing units would have to be regenerated to pick up the
change.

## See also

- ADR-0008: pexpect for /usage capture (this fix is downstream of the
  capture-via-pexpect choice).
- ADR-0013: pre/post-dispatch hooks (the operator-side worktree-create
  hook also benefits from a working TERM since it spawns Claude
  agents).
- The PR landing this change also fixed:
  - `install --config` propagation into ExecStart (was accepted but
    dropped on the floor),
  - the v2.1.141 "Quick safety check" trust prompt in
    `capture.py` Phase 1,
  - pyte-collapsed header tokens in `parser.py`
    (`Currentsession` / `Currentweek(allmodels)` now classify
    correctly via `\s*` between words).
