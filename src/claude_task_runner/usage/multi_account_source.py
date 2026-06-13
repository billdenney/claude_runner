"""Round-robin ``UsageSource`` that captures one account per tick.

PR 1-7 wired multi-account dispatch but kept a single-source
``/usage`` capture: the supervisor only observed
``settings.claude.config_dir``'s account. Other configured accounts
stayed cold-start in ``snapshot.accounts[*]``, which let
``choose_account`` over-route to them (their ``last_5h_util_pct``
appeared as 0).

This module closes that gap. :class:`MultiAccountUsageSource` wraps
one inner :class:`UsageSource` per account; each call to ``read()``
picks the account with the oldest ``last_capture_at`` (or never-
captured) and returns its reading tagged with
:attr:`UsageReading.account`. Per-account exceptions are wrapped in
a way that lets the daemon route them to that account's state
without breaking sibling captures.

Scheduling
----------
The source is driven by ``snapshot.accounts[*].last_capture_at``,
which the daemon updates after each successful read. The picker
sorts accounts by ``(last_capture_at or epoch)`` ascending and reads
the head. This is a strict round-robin in steady state and a
deterministic order on cold start (driven by the snapshot's account
key order — which the daemon seeds from
``settings.accounts``).

Backward compatibility
----------------------
Single-account configurations don't construct this class; the daemon
keeps using ``ClaudeUsageSource`` / ``ApiUsageSource`` /
``ApiThenTtyUsageSource`` directly. Only ``len(settings.accounts) >
1`` triggers the multi-account path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from claude_task_runner.usage.models import UsageReading
from claude_task_runner.usage.source import UsageSource

logger = logging.getLogger(__name__)


class MultiAccountSourceError(Exception):
    """Wraps a per-account inner-source failure with the account name.

    The previous implementation attached the account by mutating the
    *caught* exception (``exc.account = name``). That breaks on
    exceptions whose instances reject new attributes (``__slots__`` /
    frozen builtins) and risks masking the original error. This
    dedicated wrapper stores the account and the original exception
    explicitly and never mutates the caught object.

    Type-preserving routing
    -----------------------
    The supervisor's :func:`safe_poll` catches inner-source failures by
    *type* (``UsageFormatDrift`` / ``UsageCaptureTimeout`` /
    ``UsageCaptureSpawnError``) and the state machine routes on type.
    To keep that contract, instances are created via :meth:`wrap`,
    which returns an object that is *also* an instance of the original
    exception's type (a small dynamically-derived subclass). The
    daemon therefore still classifies the failure exactly as it would
    have without multi-account wrapping, while ``.account`` and
    ``.original`` are available for attribution.
    """

    #: The configured account whose inner source raised.
    account: str
    #: The exception the inner source raised.
    original: BaseException

    def __init__(self, account: str, original: BaseException) -> None:
        super().__init__(f"account {account!r} usage capture failed: {original}")
        self.account = account
        self.original = original

    @classmethod
    def wrap(cls, account: str, original: Exception) -> Exception:
        """Return a wrapper that is also an instance of ``type(original)``.

        Re-raising the returned object preserves the original
        exception's type for the supervisor's type-based routing while
        carrying ``.account`` / ``.original`` for attribution. The
        original exception is never mutated.
        """
        derived = _DERIVED_CACHE.get(type(original))
        if derived is None:
            derived = type(
                f"{cls.__name__}[{type(original).__name__}]",
                (cls, type(original)),
                {},
            )
            _DERIVED_CACHE[type(original)] = derived
        wrapper = derived.__new__(derived)
        MultiAccountSourceError.__init__(wrapper, account, original)
        return wrapper


# Cache of dynamically-derived ``MultiAccountSourceError`` subclasses,
# keyed by the original exception's type. Bounded by the (small) set of
# inner-source exception types — one entry per distinct failure class.
_DERIVED_CACHE: dict[type[BaseException], type[MultiAccountSourceError]] = {}


class _SnapshotView(Protocol):
    """Just the slice of :class:`SupervisorSnapshot` we need.

    Declared inline so this module doesn't import the supervisor
    package (which depends on this one transitively via the daemon).
    """

    @property
    def accounts(self) -> dict[str, _AccountStateView]: ...


class _AccountStateView(Protocol):
    last_capture_at: datetime | None


class MultiAccountUsageSource:
    """Captures one account per ``read()`` in round-robin order.

    Parameters
    ----------
    per_account_sources
        Map from account name to its inner ``UsageSource``. The keys
        must match ``snapshot.accounts.keys()`` so the picker can
        consult per-account ``last_capture_at``.
    snapshot_getter
        Zero-arg callable returning the current
        :class:`SupervisorSnapshot`. We can't hold a snapshot reference
        directly because the daemon mutates it via ``model_copy`` —
        each read needs the freshest ``last_capture_at`` values.

    The class is stateless across constructions; all scheduling state
    lives in the snapshot. That keeps restarts honest: a fresh
    supervisor sees the same per-account capture clocks the previous
    one wrote.
    """

    def __init__(
        self,
        per_account_sources: dict[str, UsageSource],
        snapshot_getter: _SnapshotGetter,
    ) -> None:
        if not per_account_sources:
            raise ValueError("MultiAccountUsageSource requires at least one inner source")
        self._per_account = dict(per_account_sources)
        self._snapshot_getter = snapshot_getter

    def read(self) -> UsageReading:
        """Capture the most-overdue account; return its reading tagged.

        On an inner-source failure, raises a
        :class:`MultiAccountSourceError` that is *also* an instance of
        the original exception's type (so the supervisor's type-based
        routing is unchanged) and carries ``.account`` / ``.original``
        for attribution. The caught exception is never mutated, so this
        is safe even for slotted/frozen exception types.
        """
        name = self._pick_next_account()
        inner = self._per_account[name]
        try:
            reading = inner.read()
        except Exception as exc:
            raise MultiAccountSourceError.wrap(name, exc) from exc
        return reading.model_copy(update={"account": name})

    def _pick_next_account(self) -> str:
        """Return the configured account whose ``last_capture_at`` is oldest.

        Tie-break is alphabetical on the account name so cold start
        (every account has ``last_capture_at=None``) is deterministic.
        """
        snapshot = self._snapshot_getter()
        epoch = datetime.fromtimestamp(0, tz=UTC)
        candidates = []
        for name in self._per_account:
            state = snapshot.accounts.get(name)
            last = state.last_capture_at if state is not None else None
            candidates.append(((last or epoch, name), name))
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]


# Type alias for the snapshot accessor callable. Annotated here so
# the public class signature stays readable; declared after the class
# so __init__'s forward reference resolves.
from collections.abc import Callable  # noqa: E402

_SnapshotGetter = Callable[[], _SnapshotView]
