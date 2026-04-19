# 0001. Default rate-limit regime is Claude Code Pro/Max/Teams

- Status: accepted
- Date: 2026-04-19
- Deciders: @billdenney

## Context

Claude exposes two very different rate-limit regimes:

- The **raw API**, enforced per organization with a token-bucket algorithm
  (RPM / ITPM / OTPM) and surfaced in `anthropic-ratelimit-*` response
  headers. Refill is continuous.
- **Claude Code subscriptions** (Pro, Max-5x, Max-20x, Teams), which
  impose a 5-hour rolling quota per user plus a 7-day weekly cap. Exact
  token limits are not publicly disclosed.

This project is built on top of the Claude Agent SDK, which drives the
same `claude` CLI that Pro/Max users already authenticate. Targeting the
subscription regime by default therefore matches where most users of this
tool will actually be.

## Decision

Ship with `regime = "pro_max"` as the default. Support the API regime as
an opt-in (`regime = "api"`) for users running with an API key.

## Alternatives considered

- **Default to API regime** — rejected: most Agent SDK users are on a
  subscription, so the default would be wrong for the common case.
- **Detect regime automatically** — rejected for v0.1: detection is
  unreliable (credentials can be set both ways) and surprising; an
  explicit config knob is clearer.

## Consequences

- **Positive:** users who pip-install the package and point it at a
  `claude`-authenticated system get working behavior with no flags.
- **Negative:** because Pro/Max limits are not published, the controller
  has to treat its budget numbers as best-effort and reconcile against
  observed usage. The `api` regime gets a more precise signal via
  headers.
- **Follow-ups:** ADR 0002 covers how we reconcile the budget against
  live observations when running on Pro/Max.
