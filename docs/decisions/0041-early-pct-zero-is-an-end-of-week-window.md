# ADR-0041: `early_pct = 0` confines an account to an end-of-week window

- **Date:** 2026-11-10
- **Status:** accepted
- **Related:** ADR-0022 (`[dispatch_pct.*]` trace-following), ADR-0038 (a
  reading may have no 5-hour window)

## Context

An account can serve two users: the operator's own interactive sessions
through the week, and the runner. The operator wants the runner to leave
such an account alone for most of the week, and then, in the last hours
before its weekly reset, use whatever the operator did not: up to the
weekly cap, as fast as the account allows.

The ADR-0022 curve cannot express that. It rises from 0 at week start to
`early_pct` at the end-of-week (EOW) elbow, `eow_time_switch` before the
reset, and then to `eow_pct` at the reset. The closest setting,
`early_pct = 0`, `eow_pct = 97`, `eow_time_switch = "12h"`, holds the
target at 0 until the elbow and then ramps it from 0 to 97 over 12 hours.
Two things go wrong:

- **The ramp starts below what the operator already used.** The weekly
  reading counts the operator's interactive use too, and the account is
  throttled while observed is above target. An account at 75% when the
  window opens waits until the ramp reaches 75%, 9.3 hours into a 12-hour
  window, and then trails the ramp. On the personal Max account, which
  burns about 4-5% of its week per hour at five tasks, that ends the week
  near 87% with 10 points unused.
- **A fresh week dispatches at 0%.** The rule throttles only when observed
  is *above* target. Just after a reset the reading is 0% and the target is
  0, so the account starts tasks at once, up to `max_concurrency`, before
  the next reading shows the use.

`early_pct = 0` had no other use: no queue or account file sets it, since
a sloped curve that targets 0 for most of the week is the case above.

## Decision

**`early_pct = 0` means "only in the last `eow_time_switch` before the
weekly reset, up to `eow_pct`".** With `early_pct = 0` the curve is a step
instead of a ramp:

- **Before the window** (elapsed fraction below `1 - eow_time_switch /
  window`), the target is 0 and the window is closed. A closed window is
  `THROTTLED_WEEKLY` whatever the reading, 0% included. The wakeup, and the
  catch-up time `account list` and the status report show, is the moment
  the window opens. The supervisor message says so and gives that time.
- **Inside the window**, from its opening to the reset, the target is a
  flat `eow_pct`. The ordinary rule applies: the account dispatches while
  observed is not above `eow_pct`, and is `THROTTLED_WEEKLY` until the
  reset once it is. The 5h bands still apply to an account that has a
  5-hour window.
- **Edges.** A zero-length window (`eow_time_switch = "0h"`) never opens,
  and the message says the account takes no tasks. A window of a week or
  more is always open, so the account runs up to `eow_pct` all week.

No setting is added. `early_pct` already accepts 0, `early_pct < eow_pct`
already holds, and every `early_pct > 0` curve is unchanged, including at
0% at week start.

`curve.window_open` and `curve.end_of_week_only` hold the rule;
`target_pct` and `elapsed_for_target_pct` give the step and its inverse,
so `decide` and `weekly_catch_up` agree on the opening time.

`account list` showed a per-account `0` as `~`, the mark for an inherited
value, because it printed `value or '~'`. It now shows `~` only for an
unset field.

## Alternatives considered

- **A new setting**, such as a per-account `dispatch_within = "12h"`. It
  says the same thing as `early_pct = 0` with `eow_time_switch`, and adds a
  key that interacts with the curve it would override.
- **Keep the ramp and fix only the 0% start.** The window would still open
  late for any account the operator also uses, which is the account this
  is for.
- **Flip `account pause` on a timer outside the runner.** It works with no
  runner change, but it needs a script and a systemd timer per account,
  and it overrides the operator's own pause marker.

## Consequences

- An operator can keep an account for the end of the week with three
  settings in its `runner-account.toml`, for example `early_pct = 0`,
  `eow_pct = 97`, `eow_time_switch = "12h"`.
- Inside the window the account runs at full concurrency until the
  reading passes `eow_pct`. Tasks still running at that moment finish and
  add to the use, so an `eow_pct` a few points under the cap leaves room
  for them.
- The `early_pct = 0` curve is not continuous, so "the target rises to
  meet observed" no longer describes every throttle; the closed window has
  its own message.

## Reversibility

Low cost. The change is confined to `throttle/curve.py` and
`throttle/decision.py`; reverting it restores the ramp for
`early_pct = 0`, and no stored state depends on it.
