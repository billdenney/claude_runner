# ADR-0038: A usage reading may have no 5-hour window

- **Date:** 2026-11-05
- **Status:** accepted
- **Related:** ADR-0008 (usage readings), ADR-0021 (the API usage source and
  its long-lived tokens), ADR-0022 (`[dispatch_pct.*]` trace-following)

## Context

`usage.api_source.ApiUsageSource` reads an account's usage from the
`anthropic-ratelimit-unified-*` headers of a minimal `/v1/messages` call. It
required four of them, `-5h-utilization`, `-5h-reset`, `-7d-utilization` and
`-7d-reset`, and raised `UsageApiHeaderMissing` when any was absent. Under
`[usage].source = "api_then_tty"` that falls through to the TTY capture,
and when that fails as well the account goes to `error_drift` and takes no
tasks.

The server reports the windows that apply to the account. A team account's
responses carry both windows:

```
anthropic-ratelimit-unified-5h-reset: 1793908800
anthropic-ratelimit-unified-5h-status: allowed
anthropic-ratelimit-unified-5h-utilization: 0.11
anthropic-ratelimit-unified-7d-reset: 1794193200
anthropic-ratelimit-unified-7d-status: allowed
anthropic-ratelimit-unified-7d-utilization: 0.61
anthropic-ratelimit-unified-fallback-percentage: 0.5
anthropic-ratelimit-unified-overage-reset: 1796083200
anthropic-ratelimit-unified-overage-status: allowed
anthropic-ratelimit-unified-overage-utilization: 0.0
anthropic-ratelimit-unified-representative-claim: five_hour
anthropic-ratelimit-unified-reset: 1793908800
anthropic-ratelimit-unified-status: allowed
```

A personal (Max) account's responses carry the 7-day window and no 5-hour
window at all, on every call:

```
anthropic-ratelimit-unified-7d-reset: 1794394800
anthropic-ratelimit-unified-7d-status: allowed
anthropic-ratelimit-unified-7d-utilization: 0.12
anthropic-ratelimit-unified-fallback-percentage: 0.5
anthropic-ratelimit-unified-overage-reset: 1796083200
anthropic-ratelimit-unified-overage-status: allowed
anthropic-ratelimit-unified-overage-utilization: 0.0
anthropic-ratelimit-unified-representative-claim: seven_day
anthropic-ratelimit-unified-reset: 1794394800
anthropic-ratelimit-unified-status: allowed
```

That is not a header rename: no other header takes the 5h headers' place,
and the representative claim names the 7-day window. The account has no
5-hour window to report, and the runner treated that as drift, so an
account at 12% of its weekly allowance took no tasks.

## Decision

**A reading's 5-hour window is optional.** `UsageReading.five_hour` is a
`WindowReading` or `None`. `None` means only that the source reported no
5-hour window. It is never a stand-in for 0%, never carries a made-up reset
time, and never stands for a failed reading, which still raises.

**The API source accepts a response with no 5-hour window, and nothing
looser.** `_headers_to_reading` gives `five_hour=None` when the response has
neither 5h header, the 7d pair is present and valid, and no
`anthropic-ratelimit-unified-<w>-utilization` header names a window outside
`5h`, `7d` and `overage`. Otherwise it raises `UsageApiHeaderMissing` as
before: when either 7d header is missing, when only one of the two 5h
headers is present, when a value cannot become a reading, and when a
window it does not know appears in a response without the 5h pair, since
that window may be the 5-hour window under a new name and must not read as
"no 5-hour limit". A response with both 5h headers ignores any other window,
as it always did. The `falling back to TTY source` messages are unchanged
for the cases that still raise.

**The TTY parser keeps requiring both windows.** The `/usage` screen shows
both, so one missing there is still drift.

**The throttle skips the 5h step.** `throttle.decision.decide` checks the
weekly trace first, as before. With no 5-hour window the `[dispatch_pct.day]`
and `[dispatch_pct.night]` 5h thresholds do not apply: the account is
`DISPATCHING` at its `max_concurrency` unless the weekly trace throttles it,
and a `THROTTLED_WEEKLY` wakeup is the catch-up time itself, with no 5h
reset to clamp it to. The supervisor still polls every
`[usage].poll_interval_s`, which the wakeup can only shorten. The
`Decision`'s `observed_5h_pct` is `None`; its band and thresholds are still
reported.

**The snapshot records `null`.** For such a reading `last_5h_util_pct` and
`last_5h_reset_at` are `None`, top level and per account, replacing what an
earlier reading left: a 5-hour window that no longer applies has no reset
either. `last_5h_util_pct` stays 0 until an account's first reading.
`supervisor.json` moves to schema v9, a version-only migration: every v8
value is still valid, and the bump makes an older supervisor refuse a v9
file with a version error rather than a validation error on the first
`null`. The `state_transition` event's `five_hour_util` is `null` too.

**Account selection ranks such an account first.** `choose_account` picks
the eligible account with the lowest 5h utilization; an account with no
5-hour window has no 5-hour limit to approach, so it ranks ahead of every
utilization, 0% included. Ties still break on weekly utilization, then
name.

**Displays say "not reported".** `usage render` and `usage healthcheck`,
`account list`, `supervisor status`, the doctor's `api_usage_source` check,
`usage refresh` and the runner-status report print "not reported" for the
5h utilization, never 0%. `usage json`, `usage parse-file`, `account list
--json` and `supervisor status --json` give `null`.

## Alternatives considered

- **Read a missing 5h pair as 0%.** It shows a number the server never
  sent, and the reading would look the same as one from an account that
  has a 5-hour window and has not used it, so the 5h bands would go on
  applying to a window that does not exist.
- **Accept any response without the 5h pair.** A rename of the 5h headers
  (to `-5hr-` or `-five_hour-`, say) would then read as "no 5-hour limit",
  and dispatch would run past a limit that exists. Requiring every
  utilization header to name a known window keeps a rename loud.
- **Keep the last 5h reading the account had.** The window no longer
  applies, so its utilization and reset would only grow staler, and the
  throttle would keep acting on them.
- **Rank an account with no 5-hour window last.** The rule picks the
  account with the most 5-hour room, and an account with no 5-hour limit
  has the most. Its weekly use is still governed by the weekly trace.
- **Leave the schema version at 8.** An older supervisor reading a file
  with a `null` would fail validation; each earlier schema change bumped
  the version so it fails with a version error instead.

## Consequences

- An account whose responses carry no 5-hour window takes tasks under its
  weekly trace (`[dispatch_pct.week]`) and its `max_concurrency`. Its 5h
  bands take effect again with the first reading that has a 5-hour window;
  there is no setting to change.
- A response carrying a window the source does not know, without the 5h
  pair, is drift until `_KNOWN_WINDOWS` in `usage/api_source.py` lists it.
- A reader of `supervisor.json` outside the runner must allow `null` in
  `last_5h_util_pct`.
- Going back to a supervisor built before this change means removing or
  hand-editing a v9 `supervisor.json`, which that supervisor refuses.
