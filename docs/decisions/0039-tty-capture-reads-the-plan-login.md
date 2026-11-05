# ADR-0039: The TTY usage capture reads the plan login, from the rendered screen

- **Date:** 2026-11-05
- **Status:** accepted
- **Supersedes:** decision 3 of ADR-0019 (the byte-marker loop in phase 1)
- **Related:** ADR-0008 (pexpect), ADR-0019 (pre-init `.claude.json`),
  ADR-0021 (long-lived token per account)

## Context

`claude-task-runner usage --account <name> healthcheck` failed for both
accounts of the nlmixr2lib queue with `capture timeout: TUI did not become
ready within 30.0s`. Claude Code was 2.1.318. Rendering the PTY stream
showed three separate faults.

1. **No ready marker.** Phase 1 waited for the bytes `shortcuts`, from the
   footer `? for shortcuts`. That footer is drawn only in manual mode. In
   any other mode it reads `⏵⏵ auto mode on (shift+tab to cycle)` (or
   `accept edits on`, `plan mode on`, ...), and 2.1.318 makes auto mode
   the default. The marker never arrived.
2. **A new dialog.** 2.1.318 can open "Try the new fullscreen renderer?"
   over the input box with `❯ 1. Yes, try it` focused, so the Enter at
   the end of `/usage\r` would opt in. The offer appears up to
   `fullscreenUpsellSeenCount` = 3 times per config dir (`M3t` in the
   bundle), about a second after the input box. Its words, like those of
   most lines, arrive as fragments separated by cursor-column moves
   (`Try\x1b[7Gthe\x1b[11Gnew...`), so no byte pattern of the phrase
   matches; ADR-0019 had already met this with the trust prompt.
3. **No plan limits.** With the TUI ready, `/usage` showed the session's
   cost, not the plan's limit bars. The capture spawned `claude` with
   `CLAUDE_CODE_OAUTH_TOKEN` set to the account's `oauth-token`, a
   `claude setup-token` token (exported since PR 15). The CLI prefers that
   variable to `.credentials.json`, and the token has the `user:inference`
   scope alone. Without `user:profile` the CLI knows no subscription (its
   banner says `Claude API`), and the usage endpoint answers 403. The
   panel then draws, for a moment, bars seeded from the process's own
   rate-limit headers or from `cachedUsageUtilization` in `.claude.json`
   (a fetch by another session of that config dir, up to an hour old),
   and removes them when its own refresh fails. Without the
   variable the CLI read each account's `.credentials.json`, whose login
   had expired with no refresh token: banner `API Usage Billing`,
   `Not logged in · Run /login`, and the same cost panel. `claude auth
   status` agreed: `oauth_token` with the variable, `loggedIn: false`
   without it. No credential on disk could show the bars.

A fourth fault sat behind the pre-trust of ADR-0019: the 2.1.318 trust
prompt focuses `No, exit`, so the Enter that phase 1 sent to it would
have ended `claude`.

## Decision

1. **Phase 1 reads the rendered screen.** The stream is fed, as it
   arrives, into a pyte screen the size of the PTY (`render.LiveScreen`;
   the PTY is now spawned at an explicit 80x24, pexpect's default). The
   input box is ready when its footer is on the screen, in any mode
   (`? for shortcuts`, or `<mode> on (… to cycle)`), the input row is
   too (a row starting with `❯` right under a rule), and no prompt is.
   Every recorded dialog takes the input row's place, so one this table
   does not know ends phase 1 in a timeout rather than taking `/usage`.
2. **Prompts are answered from a table.** The trust prompt gets the
   option `Yes, I trust this folder`: 2.1.318 focuses `No, exit`, so
   phase 1 sends Down until the focus marker is on "Yes", then Enter
   (the old Enter alone would have ended `claude`). The theme picker gets
   Enter, which keeps the highlighted theme; it has no numbers any more,
   and the old `1` plus a line feed could leak a key into the next
   screen. The fullscreen offer, and any other dialog with the
   `Enter to confirm · Esc to cancel` footer, gets Esc, which never opts
   in; trust comes before that rule because Esc on it is "No, exit".
   Keys go only after a poll in which `claude` drew nothing, so they
   answer a whole frame, and Esc or Down goes again after 2 s without
   a change: in its first seconds 2.1.318 redraws the trust prompt with
   the focus reset and drops a key sent before. Enter is never repeated:
   on a TUI that acted on it but has not redrawn yet, a second Enter
   would land on the next screen, possibly the fullscreen offer. A prompt still drawn at
   the timeout is named in it. After the post-ready pad phase 1 runs
   again, so a dialog that opened in the pad (the offer appears about a
   second after the input box) is closed before `/usage` is typed. The
   bound of eight keys spans the whole capture, both runs of phase 1
   together.
3. **`ensure_initialized` raises `fullscreenUpsellSeenCount` to 3**, so
   the offer stays closed; an integer count of 3 or more is kept, any
   other value becomes 3. The Esc rule is the fallback for a CLI that
   counts differently.
4. **The capture authenticates with the account's login, never the
   long-lived token.** `_build_spawn_env` drops `CLAUDE_CODE_OAUTH_TOKEN`,
   both the `oauth-token` file and an inherited value. The dispatcher and
   the API source still use the file; ADR-0021 is otherwise unchanged.
5. **A banner that names API billing stops the capture at once** with
   `UsageCaptureLoginRequired`, before `/usage` is typed; the banner is
   read on the screen after the pad, by which time the `Not logged in`
   notice is drawn too. The first-run "Select login method" screen of a
   config dir with no login raises the same error. Its message names the
   config dir, what the screen showed, `.credentials.json` and the step: a
   `/login` in `CLAUDE_CONFIG_DIR=<dir> claude`, in a shell without the
   token variable. The class derives from `UsageApiAuthExpired`, so the
   supervisor treats it as a login failure (`ErrorDrift`, one
   notification) rather than a capture to retry each tick, and the CLI
   commands that catch `UsageCaptureSpawnError` report it.
6. **A failed capture keeps its `.cap`.** What `claude` drew is written
   to `usage_captures/` whenever the capture fails after the spawn, not
   only on success.

## Alternatives considered

- **Pin the mode with `--permission-mode`.** 2.1.318's choices are
  `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk` and
  `plan`; `default` is gone. A flag whose values change between releases
  would make the spawn itself fail on a CLI that renamed one, and the
  footer is mode-specific text either way.
- **Keep the token and read the bars the panel draws first.** They are
  another session's fetch, up to an hour old, and vanish within seconds;
  the personal account's showed only the weekly window. The throttle
  would steer by numbers the CLI itself withdrew.
- **Detect the cost panel after `/usage`.** In 2.1.318 the panel that
  draws limit bars opens with the same session-cost section, so the
  panel does not tell the two apart; the banner does, and it is on
  screen before any key is sent.
- **Match the dialog in the bytes.** The phrases arrive in fragments; a
  single-word marker (ADR-0019) would need a new guess for each dialog.

## Consequences

- An account without a current claude.ai login fails its TTY capture
  within the post-ready pad plus about a second (3.3 s for both accounts
  with a 2 s pad) with the step to fix it, instead of 30 s of timeout
  plus a skipped tick. For the nlmixr2lib queue that is both accounts
  until `/login` is run for each config dir (runbook: "A usage capture
  says claude has no claude.ai login").
- Accounts read through the API alone (`api_then_tty` with a long-lived
  token, ADR-0021) are unaffected; the TTY path is the `usage` CLI and
  accounts without an `oauth-token`.
- An account on `[usage].source = "tty"` with only a long-lived token now
  stops at `ErrorDrift` with the login message, where it used to time
  out on every tick.
- `fullscreenUpsellSeenCount` is 3 in every config dir the runner
  spawns `claude` with, interactive sessions included.

## Known limits

- A dialog drawn over the input box that leaves both the input row and
  the footer visible counts as ready, and `/usage` is typed into it.
  Phase 2 then times out, so it is loud, not silent.
- A dropped Enter is not repeated: the capture times out naming the
  prompt, and the next capture tries again.

## Reversibility

Low. Each part stands alone: the token export, the `.claude.json` key,
the banner check and the screen-based phase 1 can each be reverted
without the others.
