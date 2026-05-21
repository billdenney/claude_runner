"""Pure dispatch policy: pick the account a task should be dispatched through.

The policy is a function of the resolved accounts (queue-side
declaration + per-account dispatch policy from
``<config_dir>/runner-account.toml``), each account's most recent
state (capacity + throttle band), the in-flight task set (per-account
utilisation), and the task itself (which may pin a specific account).

Kept side-effect-free so the supervisor's tick loop can call it
without touching the disk and the unit tests can drive it with
hand-built inputs.

Selection rule
--------------
1. If ``task.account`` is set, route to that account when capacity is
   available; otherwise reject (the orchestrator surfaces the conflict
   in the dispatch log).
2. Otherwise: filter to accounts that are dispatching-eligible
   (state in DISPATCHING / SLOWING_DOWN / END_OF_WEEK_PUSH / IDLE,
   not paused, has capacity).
3. All accounts are equal priority. Pick the one with the lowest
   ``last_5h_util_pct``. Tie-break by ``last_weekly_util_pct``, then
   account name (lexicographic, deterministic).

There is intentionally no queue-wide concurrency cap: each account's
``max_concurrency`` (from its own ``runner-account.toml``) is the only
ceiling. The operator chooses per-account caps such that their sum is
acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass

from claude_task_runner.config.schema import ResolvedAccount
from claude_task_runner.queue.schema import Task
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorState,
)

_DISPATCHABLE_STATES: frozenset[SupervisorState] = frozenset(
    {
        SupervisorState.DISPATCHING,
        SupervisorState.SLOWING_DOWN,
        SupervisorState.END_OF_WEEK_PUSH,
        SupervisorState.IDLE,
    }
)
"""States where an account is willing to take a new dispatch.

IDLE is included so cold-start dispatches don't stall waiting for the
first ``/usage`` capture to land — the next tick reclassifies the
account and pulls it out of IDLE."""


@dataclass(frozen=True)
class DispatchChoice:
    """Outcome of :func:`choose_account`.

    ``account`` is the name to dispatch through; ``None`` means the
    policy declined. ``reason`` is a short human-readable string
    explaining the choice or the decline — surfaced in the dispatch
    log so the operator can audit per-tick decisions.
    """

    account: str | None
    reason: str


def choose_account(
    *,
    task: Task,
    accounts: dict[str, ResolvedAccount],
    account_states: dict[str, AccountState],
    in_flight: list[InFlightRecord],
) -> DispatchChoice:
    """Pick the account ``task`` should be dispatched through.

    Pure function: no I/O, no clock, deterministic per inputs.

    Parameters
    ----------
    task
        The task being dispatched. ``task.account`` (when set) pins it
        to a specific account.
    accounts
        Resolved accounts keyed by name (from
        ``loader.resolve_accounts(settings)``). Each carries the queue-
        side declaration plus the per-account
        :class:`AccountPolicy` (max_concurrency + throttle bands).
    account_states
        Per-account state keyed by name (from
        ``SupervisorSnapshot.accounts``).
    in_flight
        Attributed in-flight tasks (from
        ``SupervisorSnapshot.in_flight``).

    Returns
    -------
    DispatchChoice
        ``account`` names the picked account, or ``None`` when no
        account has capacity. ``reason`` is a short audit string.
    """
    pinned = task.account
    if pinned is not None:
        if pinned not in accounts:
            return DispatchChoice(
                account=None,
                reason=f"pinned to unknown account {pinned!r}",
            )
        if pinned not in account_states:
            return DispatchChoice(
                account=None,
                reason=f"pinned account {pinned!r} has no state yet (cold start)",
            )
        state = account_states[pinned]
        acct = accounts[pinned]
        if state.paused:
            return DispatchChoice(
                account=None,
                reason=f"pinned account {pinned!r} is paused",
            )
        if state.state not in _DISPATCHABLE_STATES:
            return DispatchChoice(
                account=None,
                reason=f"pinned account {pinned!r} is {state.state.value}",
            )
        if not _has_capacity(pinned, acct, in_flight):
            return DispatchChoice(
                account=None,
                reason=f"pinned account {pinned!r} at capacity",
            )
        return DispatchChoice(account=pinned, reason=f"pinned to {pinned!r}")

    candidates: list[tuple[tuple[int, int, str], str]] = []
    blocked_reasons: list[str] = []
    for name, acct in accounts.items():
        maybe_state = account_states.get(name)
        if maybe_state is None:
            blocked_reasons.append(f"{name}: no state (cold start)")
            continue
        if maybe_state.paused:
            blocked_reasons.append(f"{name}: paused")
            continue
        if maybe_state.state not in _DISPATCHABLE_STATES:
            blocked_reasons.append(f"{name}: {maybe_state.state.value}")
            continue
        if not _has_capacity(name, acct, in_flight):
            blocked_reasons.append(f"{name}: at capacity")
            continue
        sort_key = (
            maybe_state.last_5h_util_pct,
            maybe_state.last_weekly_util_pct,
            name,
        )
        candidates.append((sort_key, name))

    if not candidates:
        return DispatchChoice(
            account=None,
            reason="no eligible account: " + "; ".join(blocked_reasons),
        )

    candidates.sort(key=lambda c: c[0])
    picked = candidates[0][1]
    return DispatchChoice(
        account=picked,
        reason=(
            f"picked {picked!r} (5h_util={account_states[picked].last_5h_util_pct}%, "
            f"weekly_util={account_states[picked].last_weekly_util_pct}%)"
        ),
    )


def _has_capacity(
    name: str,
    acct: ResolvedAccount,
    in_flight: list[InFlightRecord],
) -> bool:
    """True iff dispatching one more task to ``name`` stays under its cap.

    The cap is ``acct.policy.concurrency.max_concurrency`` (loaded from
    the account's own ``runner-account.toml``; defaults to 1 if absent).
    There is no queue-wide ceiling.
    """
    used = sum(1 for r in in_flight if r.account == name)
    return used < acct.policy.concurrency.max_concurrency


def account_in_flight_count(account: str, in_flight: list[InFlightRecord]) -> int:
    """Count in-flight tasks attributed to ``account``.

    Surfaced as a helper because ``account list`` (CLI in PR4) and the
    orchestrator's eligibility check both need it.
    """
    return sum(1 for r in in_flight if r.account == account)
