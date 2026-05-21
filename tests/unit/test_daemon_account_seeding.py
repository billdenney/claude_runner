"""Tests for v3 snapshot seeding with per-account state.

The daemon's startup path calls ``persist_mod.initial_snapshot(
since=..., account_names=[a.name for a in settings.accounts])`` so the
first tick's snapshot has one ``AccountState`` per configured account.
Without this seeding the snapshot has a single ``"default"`` entry and
the orchestrator's ``choose_account`` rejects every dispatch with
"no state (cold start)".

These tests pin the snapshot-seeding contract without standing up a
full daemon (which needs the global lock and a real UsageSource).
"""

from __future__ import annotations

from datetime import UTC, datetime

from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import SupervisorState


def test_initial_snapshot_seeds_named_accounts() -> None:
    """``account_names=[...]`` produces one ``AccountState`` entry per name."""
    snap = persist_mod.initial_snapshot(
        since=datetime(2026, 5, 21, tzinfo=UTC),
        account_names=["personal", "work"],
    )
    assert sorted(snap.accounts.keys()) == ["personal", "work"]
    for state in snap.accounts.values():
        assert state.state is SupervisorState.IDLE
        assert state.paused is False


def test_initial_snapshot_defaults_to_legacy_single_account() -> None:
    """Omitting ``account_names`` keeps the v2 behaviour: one "default"."""
    snap = persist_mod.initial_snapshot(since=datetime(2026, 5, 21, tzinfo=UTC))
    assert list(snap.accounts.keys()) == ["default"]


def test_initial_snapshot_explicit_default_only() -> None:
    """``account_names=["default"]`` produces exactly that single entry,
    matching what the loader synthesises for a legacy
    ``[claude].config_dir`` config."""
    snap = persist_mod.initial_snapshot(
        since=datetime(2026, 5, 21, tzinfo=UTC),
        account_names=["default"],
    )
    assert list(snap.accounts.keys()) == ["default"]
