"""Pure dispatch policy: pick the account a task should be dispatched through.

The policy is a function of the resolved accounts (queue-side
declaration + per-account dispatch policy from
``<config_dir>/runner-account.toml``), each account's most recent
state (capacity + throttle band), the in-flight task set (per-account
utilisation), the task itself (which may pin a specific account), and
the **affined account** (which hosts the task's current claude session,
if any — see ADR-0024).

Kept side-effect-free so the supervisor's tick loop can call it
without touching the disk and the unit tests can drive it with
hand-built inputs.

Selection rule
--------------
1. If ``affined_account`` is set, route to that account when it exists
   and has capacity; otherwise reject. Affinity is a correctness gate,
   not a policy choice — a Claude session created on one account is
   invisible to ``claude`` running with a different ``CLAUDE_CONFIG_DIR``,
   so resuming elsewhere yields ``No conversation found with session ID``.
   When affinity is set, ``task.account`` pinning is ignored (the
   pinning happened at task-author time, before the session existed;
   the session's host is the binding constraint until an operator runs
   ``queue restart-fresh`` to clear it).
2. Else if ``task.account`` is set, route to that account when capacity
   is available; otherwise reject (the orchestrator surfaces the
   conflict in the dispatch log).
3. Otherwise: filter to accounts that are dispatching-eligible
   (state in DISPATCHING / SLOWING_DOWN / IDLE, not paused, has
   capacity).
4. All accounts are equal priority. Pick the one with the lowest
   ``last_5h_util_pct``. Tie-break by ``last_weekly_util_pct``, then
   account name (lexicographic, deterministic).

Capacity
--------
An account has capacity while its in-flight count is below its cap: its
``max_concurrency`` (from its own ``runner-account.toml``), lowered to
the ``target_concurrency`` of its last throttle decision when that is
set. While SLOWING_DOWN that target is ADR-0022's linear ramp, the
number the supervisor announced, so each account slows down on its own
5h reading.

This module applies no queue-wide cap. The orchestrator separately
bounds the queue's total in-flight tasks by ``[concurrency]``
(``runner.orchestrator._target_concurrency``).
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
        SupervisorState.IDLE,
    }
)
"""States where an account is willing to take a new dispatch.

IDLE is included so cold-start dispatches don't stall waiting for the
first ``/usage`` capture to land, and so tasks that arrive after the
queue emptied don't wait for the account's next capture: on a
multi-account queue that is a full round-robin cycle away. A captured
IDLE account keeps the ``target_concurrency`` of the reading that idled
it, which :func:`account_cap` applies."""


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
    affined_account: str | None = None,
) -> DispatchChoice:
    """Pick the account ``task`` should be dispatched through.

    Pure function: no I/O, no clock, deterministic per inputs.

    Parameters
    ----------
    task
        The task being dispatched. ``task.account`` (when set) pins it
        to a specific account (ignored when ``affined_account`` is set).
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
    affined_account
        Name of the account that hosts the task's current Claude
        session (from ``TaskState.session_host_account()``), or
        ``None`` when there's no session yet. Multi-account queues
        must resume sessions on the account that created them
        (sessions are namespaced by ``CLAUDE_CONFIG_DIR``); affinity
        is enforced as a correctness gate ahead of ``task.account``
        pinning. See ADR-0024.

    Returns
    -------
    DispatchChoice
        ``account`` names the picked account, or ``None`` when no
        account has capacity. ``reason`` is a short audit string.
    """
    if affined_account is not None:
        if affined_account not in accounts:
            return DispatchChoice(
                account=None,
                reason=(
                    f"session affinity blocks dispatch: host account "
                    f"{affined_account!r} not in [[accounts]] (run "
                    "`queue restart-fresh` to start a fresh session)"
                ),
            )
        if affined_account not in account_states:
            return DispatchChoice(
                account=None,
                reason=(
                    f"session affinity: host account {affined_account!r} "
                    "has no state yet (cold start)"
                ),
            )
        state = account_states[affined_account]
        acct = accounts[affined_account]
        if state.paused:
            return DispatchChoice(
                account=None,
                reason=(
                    f"session affinity blocks dispatch: host account {affined_account!r} is paused"
                ),
            )
        if state.state not in _DISPATCHABLE_STATES:
            return DispatchChoice(
                account=None,
                reason=(
                    f"session affinity blocks dispatch: host account "
                    f"{affined_account!r} is {state.state.value}"
                ),
            )
        if not _has_capacity(affined_account, acct, state, in_flight):
            return DispatchChoice(
                account=None,
                reason=(
                    f"session affinity blocks dispatch: host account "
                    f"{affined_account!r} at capacity "
                    f"({_capacity_text(affined_account, acct, state, in_flight)})"
                ),
            )
        return DispatchChoice(
            account=affined_account,
            reason=f"session affined to {affined_account!r}",
        )

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
        if not _has_capacity(pinned, acct, state, in_flight):
            return DispatchChoice(
                account=None,
                reason=(
                    f"pinned account {pinned!r} at capacity "
                    f"({_capacity_text(pinned, acct, state, in_flight)})"
                ),
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
        if not _has_capacity(name, acct, maybe_state, in_flight):
            blocked_reasons.append(
                f"{name}: at capacity ({_capacity_text(name, acct, maybe_state, in_flight)})"
            )
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


def account_cap(acct: ResolvedAccount, state: AccountState) -> int:
    """How many tasks ``acct`` may run at once right now.

    ``acct.policy.concurrency.max_concurrency`` (from the account's own
    ``runner-account.toml``; 1 if absent), lowered to
    ``state.target_concurrency`` when the account's last throttle
    decision set one. While SLOWING_DOWN that is the ramp target the
    supervisor announced. An IDLE account keeps the target of the reading
    that idled it, 0 if that reading was throttled; only an account never
    captured has none.
    """
    cap = acct.policy.concurrency.max_concurrency
    if state.target_concurrency is not None:
        cap = min(cap, state.target_concurrency)
    return cap


def _has_capacity(
    name: str,
    acct: ResolvedAccount,
    state: AccountState,
    in_flight: list[InFlightRecord],
) -> bool:
    """True iff dispatching one more task to ``name`` stays under :func:`account_cap`."""
    return account_in_flight_count(name, in_flight) < account_cap(acct, state)


def _capacity_text(
    name: str,
    acct: ResolvedAccount,
    state: AccountState,
    in_flight: list[InFlightRecord],
) -> str:
    """``in-flight/cap`` for a decline reason, e.g. ``2/2``."""
    return f"{account_in_flight_count(name, in_flight)}/{account_cap(acct, state)}"


def account_in_flight_count(account: str, in_flight: list[InFlightRecord]) -> int:
    """Count in-flight tasks attributed to ``account``.

    Surfaced as a helper because ``account list`` (CLI in PR4) and the
    orchestrator's eligibility check both need it.
    """
    return sum(1 for r in in_flight if r.account == account)
