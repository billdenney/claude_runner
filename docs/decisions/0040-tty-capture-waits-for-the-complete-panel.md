# ADR-0040: The TTY capture waits for the complete `/usage` panel, which may have no 5-hour window

- **Date:** 2026-11-05
- **Status:** accepted
- **Supersedes:** the paragraph "The TTY parser keeps requiring both
  windows" of ADR-0038
- **Related:** ADR-0008 (pexpect), ADR-0038 (a reading without a 5-hour
  window), ADR-0039 (the TTY capture reads the plan login from the screen)

## Context

With both accounts of the nlmixr2lib queue logged in to claude.ai
(ADR-0039), `claude-task-runner usage --account <name> healthcheck` passed
for the work account and failed for the personal one with `capture timeout:
only saw 1 of 2 Resets lines within 30.0s`. The personal plan has no
5-hour window, which the API source already reads as `five_hour=None`
(ADR-0038). Its `/usage` panel, rendered from the failed capture's `.cap`:

```
Current week (all models)
████████                                           16% used
Resets Nov 11, 11am (UTC)

Current week (Fable)
██████                                             12% used
Resets Nov 11, 11am (UTC)

Usage credits
Unlimited

Esc to cancel
```

Three faults met there, and the second affected every account.

1. **Phase 2 counted bytes.** It waited for the bytes `Resets` to arrive
   twice. The screen above has two `Resets` lines, but claude 2.1.318
   redraws only the cells that change: it wrote the second as `Rese`, a
   cursor move, then `s`, keeping the `t` that `Esc to cancel` had left
   in that cell. The count was never a count of lines.
2. **Two `Resets` lines came before the panel's own numbers.** The panel
   opens with the numbers it last saved (`cachedUsageUtilization` in
   `.claude.json`, or the process's own rate-limit headers) under a
   `Refreshing…` line, and draws its fetch's numbers a frame later. The
   work panel's first frame already holds two `Resets` lines, session and
   weekly, and the capture returned the earliest snapshot with two blocks:
   the saved numbers, without the `Current week (Fable)` section the fetch
   adds. The personal panel opened at 15% and its fetch drew 16%; a later
   work capture opened at 16% for the session and its fetch drew 17%.
3. **The parser required the `Current session` section.** ADR-0038 kept
   that requirement for the TTY path. Given the panel above, its
   positional fallback would have taken the weekly block for the 5-hour
   window too, and the parse would have failed as one distinct block.

The panel's states, from the 2.1.318 bundle and the captures:

| State | Drawn above the closing line | Closing line |
|---|---|---|
| no numbers yet | `Loading usage data…` | `Esc to cancel` |
| saved numbers, fetch in flight | the sections, `Refreshing…` | `Esc to cancel` |
| fetched | the sections | `Esc to cancel` |
| fetch failed, saved numbers kept | the sections, `Could not refresh usage data` or `Showing last-known usage …` | `r to retry · Esc to cancel` |
| fetch failed, no numbers | `Error: …` | `r to retry · Esc to cancel` |

A section is drawn only for a window the fetch returned a utilization for;
`Current session` is the 5-hour window. Nothing retries a failed fetch
unless `r` is pressed.

## Decision

1. **Phase 2 waits for the complete panel on the rendered screen.** The
   whole stream is fed to a second virtual terminal the size the parser
   renders at (200x100), so the panel judged complete is the one the
   parser reads. The panel is complete when, on a screen `claude` drew
   nothing new on for a poll (0.25 s), its closing line reads `Esc to
   cancel` alone and neither `Refreshing…` nor `Loading usage data…` is
   drawn (`parser.panel_pending`). That holds however many sections the
   plan has. At every frame end of both 2026-11-05 captures the panel is
   complete only after its fetch has drawn, and parses there as the whole
   capture does; the tests pin that. A line test alone is not enough:
   byte prefixes inside a frame pass it (the personal panel's last frame
   writes the Fable heading over `Refreshing…` before it draws the
   gauge), so the panel is judged only on a quiet screen. claude writes a
   frame at once, so a poll without bytes falls between frames.
2. **A failed fetch stops the capture at once.** A closing line that
   offers a retry raises `UsageCaptureTimeout` naming the line above it,
   such as `Could not refresh usage data`. Waiting would only reach the
   deadline, and like a slow fetch it is a failure to retry at the next
   tick, not drift. Every other wait is bounded by
   `[usage].capture_usage_timeout_s`, whose timeout says what the panel
   still lacked, and an exit of `claude` is a timeout too.
3. **The pad stays.** After the panel is complete the capture still
   reads for `[usage].capture_post_data_pad_ms`. The stream after the pad
   is the one returned when its panel is still complete and shows every
   section the complete one did, so numbers redrawn in the pad win;
   otherwise the stream at completion is. The stream after the cleanup's
   Esc is never returned.
4. **The TTY parser reads a panel without a 5-hour window, on the
   screen's evidence.** A panel with a `Current week (all models)` section
   and no `Current session` block reads with `five_hour=None` only when
   all of these hold (`_no_five_hour_section_doubts`):
   - the panel is complete (decision 1), its fetch did not fail, and it
     was not dismissed (`dialog dismissed` on the screen): an unfinished
     panel may lack sections its fetch would draw, and after the cleanup's
     Esc the render keeps only the panel's lower rows;
   - no 5-hour heading (`Current session`, `5-hour session`, `5h session`)
     is anywhere on the screen, which would be a 5-hour section without
     its percentage or reset;
   - every `NN% used` gauge sits under a weekly heading (`Current week
     (…)`) or a credit heading (one ending in `credit` or `credits`, such
     as `Usage credits` with a monthly limit). A gauge under any other
     heading may be the 5-hour window renamed.

   This mirrors the API source's rule (ADR-0038) as far as the screen
   allows: the weekly section for the 7d pair, no 5-hour heading for no
   header naming `5h`, only known sections for only known windows. The
   screen has nothing like the representative claim, so the rule rests
   on the other checks and on the panel being complete. Otherwise the
   parse is `UsageFormatDrift`, with a message that lists each failed
   check after `5-hour usage block not found, and the panel does not show
   that no 5-hour window applies:`. Per-model sections such as `Current
   week (Fable)` go to `extra_windows`, as before.
5. **Position stands in only for a heading the parser cannot read.** A
   panel whose sections carry no recognised heading still reads its
   first block as the 5-hour window and its second as the weekly one. A
   block under a heading that names another window never stands in: a
   panel with `Current session` and `Current week (Fable)` but no
   all-models section is drift, where it used to read Fable as the
   weekly window.

## Alternatives considered

- **Count `Resets` lines on the rendered screen.** It fixes the byte
  count but not the early stop: the work panel's saved frame already has
  two, and a weekly-only plan without a per-model section would have one.
- **Wait for a quiet period alone.** The saved-numbers frame is followed
  by a network round trip, often longer than any quiet period worth
  waiting for, so the capture would stop on it.
- **Wait for the closing line alone, or for `Usage credits`.** The closing
  line is drawn in the first frame, under the saved numbers. `Usage
  credits` is drawn only for some plans, and only when the fetch returns
  extra-usage data.
- **Read `five_hour=None` whenever `Current session` is missing.** A
  renamed heading, a session section drawn without a reset, a panel still
  showing saved numbers, and the render of a dismissed panel (which keeps
  the weekly sections and loses the session above them) would all read as
  "no 5-hour limit", and dispatch would run past a limit that exists.
- **Keep the positional fallback for the 5-hour window when a weekly
  heading is present.** It would read a renamed session section as the
  5-hour window by its place, the guess ADR-0038 refused for the API.

## Consequences

- An account whose plan has no 5-hour window reads through the TTY
  capture too, with `five_hour=None`; the throttle then uses its weekly
  trace alone (ADR-0038). `usage healthcheck` prints `5h=not reported`.
- Every TTY reading is the panel's own fetch, not numbers saved up to an
  hour before. A capture takes one poll longer once the fetch lands.
- A failed fetch is a capture timeout at once: a `usage_capture_error`
  event, and the account keeps its last clean reading until
  `[usage].max_reading_age_s`.
- A TUI that renames `Refreshing…`, `Loading usage data…` or `Esc to
  cancel` makes every capture time out, naming what the panel still
  lacked, until `usage/parser.py` learns the new text.
- `usage parse-file` on a forensics `.cap` reads the cleanup too. With
  2.1.318 that render loses the panel's top rows, so a panel without a
  session section reports drift (`the panel was dismissed`); the bytes
  `usage capture --save` writes stop before the cleanup.

## Known limits

- The screen cannot show that a 5-hour window was withheld: a TUI that
  stopped drawing `Current session` with no other trace would read as no
  5-hour window. Under `api_then_tty` the API source reads the same
  account independently.

## Reversibility

Medium. The parser's rule and the capture's wait are separate: the rule
can be reverted to require `Current session` without touching the wait,
and the wait relies only on `parser.panel_pending` and
`parser.panel_refresh_failure`.
