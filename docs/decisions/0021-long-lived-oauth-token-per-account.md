# ADR-0021: Per-account long-lived OAuth token via `claude setup-token`

- **Date:** 2026-05-23
- **Status:** proposed
- **Supersedes:** none
- **Related:** ADR-0007 (fresh schema), PR 6 (ApiUsageSource), PR 8 (multi-account capture)

## Context

On 2026-05-23 the live queue's `work` account stopped dispatching at
~23:48 UTC the previous evening. Investigation found:

- `~/.claude/.credentials.json` accessToken had expired ~16h earlier.
- Direct refresh-token grant against
  `https://platform.claude.com/v1/oauth/token` returned
  `{"error":"invalid_grant", "error_description":"Refresh token not
  found or invalid"}` — same response from
  `https://api.anthropic.com/v1/oauth/token`. The refresh token was
  revoked (most likely rotated by a parallel login on another device).
- The runner's `ApiThenTtyUsageSource` swallowed the 401 and fell
  through to TTY. The TTY spawn `claude /usage` then timed out
  (waiting for the `/usage` page to render two `Resets` lines that
  never arrived — because the CLI was itself 401-ing on its first
  API call). The supervisor saw `UsageCaptureTimeout`, attributed it
  to a generic capture failure, and left the account pinned at the
  last good utilization (100% 5h) — the THROTTLED_5H state from
  before the bearer died.

Operator-visible symptom: account stuck in `throttled_5h` indefinitely
with no usage drop, no journald log naming OAuth, and no
`runner-status` cue. The operator had to do an interactive
`claude /login` to recover.

This ADR introduces two changes to make the OAuth lifecycle
robust against this specific failure (revoked refresh token), plus
visible when it inevitably reappears.

## Decision

### 1. Long-lived OAuth token file per account

Each account's `<config_dir>/oauth-token` file holds an
`sk-ant-oat01-…` long-lived token minted by `claude setup-token`:

- ~1-year lifetime.
- "Inference-only" scope — sufficient for dispatching tasks and for
  the `ApiUsageSource` `/v1/messages` probe; not for "Remote Control"
  flows (Claude Code's name for a class of interactive-only features
  the runner doesn't use).
- One-line plain text (no JSON wrapper); whitespace stripped on read.
- Permissions: operator's responsibility (the runner warns once on
  world-readable files, similar to the heuristic Claude Code uses
  for `.credentials.json`).

When the file is present:

- `ApiUsageSource` uses it instead of `.credentials.json`'s
  short-lived accessToken.
- `dispatcher.py` sets `CLAUDE_CODE_OAUTH_TOKEN=<token>` on every
  spawned `claude` subprocess for that account — the CLI honors
  this env var as the auth source (same contract documented for
  GitHub Actions).
- The supervisor's per-account source selection drops the TTY
  composite for that account: TTY cannot recover a revoked
  long-lived token either (the CLI uses the same bearer), so the
  right behavior is to surface the failure rather than mask it.

When the file is absent (single-account / pre-PR-14 deployments),
the runner falls back to `.credentials.json` parsing verbatim.

### 2. `UsageApiAuthExpired` → `ERROR_DRIFT`

PR 6 defined `UsageApiAuthExpired` as a subclass of
`UsageCaptureSpawnError` so the daemon's existing `safe_poll` would
route it correctly. In practice that routing meant "skip this tick,
state unchanged, monitor in-flight only" — which is wrong for
auth failures: no amount of retry will fix a revoked bearer.

State-machine routing change:

- Auth-expired enters `ERROR_DRIFT` (the same target state as
  parser format drift).
- `last_drift_message` is populated with the source's exception
  text (e.g. `"OAuth bearer rejected by … (HTTP 401)"`).
- One `Notify(level="error")` action fires on entry (not on every
  subsequent tick).
- `StopDispatch` action halts new work; in-flight tasks are not
  killed (architectural invariant 2).
- `oauth_auth_expired` event is emitted on every auth-expired tick
  for log correlation.

Recovery from `ERROR_DRIFT` after an auth-expired requires the same
N consecutive clean polls the parser-drift recovery uses — once the
operator's `claude setup-token` (or `claude /login`) replaces the
bearer and the next capture succeeds, the supervisor returns to
`DISPATCHING` after `settings_usage.drift_recovery_clean_polls`
clean polls.

## Consequences

- **Operator-visible failure:** the next time an OAuth bearer dies
  (long-lived or short-lived), it surfaces in `runner-status` as
  `error_drift` with a message naming OAuth, instead of looking
  like a benign throttle.
- **Recovery without daily refresh:** with long-lived tokens, the
  supervisor never refreshes — the operator runs `claude
  setup-token` once a year. No proactive-refresh watchdog needed
  for long-lived accounts.
- **Backward compatible:** when `<config_dir>/oauth-token` is absent
  the supervisor behaves exactly as PR 13.
- **Sudo path:** the multi-Linux-user dispatch (PR 3) propagates
  `CLAUDE_CODE_OAUTH_TOKEN` across the sudo boundary via the
  explicit `env` wrapper, matching how `CLAUDE_CONFIG_DIR` is
  already propagated. No new sudo configuration needed.
- **Limitation:** when a long-lived token is itself revoked
  (e.g. operator-initiated logout from claude.ai), the supervisor
  still requires an interactive `claude setup-token` cycle to
  recover. The token is by definition not refreshable — that's the
  trade-off of the 1-year lifetime + inference-only scope.

## Alternatives considered

- **Proactive refresh of short-lived tokens before expiry.** Would
  prevent the access-token-expiry case but not the
  refresh-token-revoked case that triggered this ADR. The
  long-lived token sidesteps both.
- **Use Anthropic Console API keys (`sk-ant-api03-…`).** These don't
  expire, but bypass the team/max subscription quota and bill
  against API credits — incompatible with the runner's
  quota-aware design.
- **Headless browser to drive the OAuth flow on every refresh.**
  Fully unattended but requires storing a persistent claude.ai
  login in a long-running browser profile on the supervisor host —
  larger security surface than a 1-year token file.
