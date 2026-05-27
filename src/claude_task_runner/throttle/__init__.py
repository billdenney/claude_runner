"""Dispatch-pct policy and decision (ADR-0022, variant-C trace-following).

The package name retains the word "throttle" because it remains the
clearest short label for the supervisor's dispatch-gating math, even
though the configuration schema no longer uses that word. Field names,
identifiers, and operator-facing strings drop ``throttle`` /
``band`` / ``pacing`` / ``slack`` / ``pause_at`` entirely.

Layers:

* :mod:`.curve` — pure piecewise-linear weekly target + its inverse.
* :mod:`.time_of_day` — wrap-aware day/night band selection.
* :mod:`.policy` — merge queue + per-account settings into a frozen
  :class:`ResolvedPolicy` with no Optional fields.
* :mod:`.decision` — pure step function:
  ``decide(policy, reading, clock) → Decision``.
"""
