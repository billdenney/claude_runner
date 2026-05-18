"""Tests for ``claude_task_runner.claude_init.ensure_initialized``."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from claude_task_runner.claude_init import ensure_initialized


def _write_claude_json(config_dir: Path, payload: dict[str, object]) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    f = config_dir / ".claude.json"
    f.write_text(json.dumps(payload, indent=2))
    os.chmod(f, 0o600)
    return f


class TestEnsureInitializedFlagFlips:
    def test_writes_both_flags_on_fresh_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "fresh-config"
        _write_claude_json(cfg, {"numStartups": 1})
        trust = tmp_path / "workspace"
        trust.mkdir()

        changed = ensure_initialized(cfg, trust)

        assert changed is True
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["hasCompletedOnboarding"] is True
        assert data["projects"][str(trust.resolve())]["hasTrustDialogAccepted"] is True

    def test_preserves_unrelated_keys(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        original = {
            "numStartups": 4186,
            "installMethod": "native",
            "tipsHistory": {"new-user-warmup": 1, "plan-mode": 42},
            "oauthAccount": {"id": "abc"},
        }
        _write_claude_json(cfg, original)
        trust = tmp_path / "workspace"
        trust.mkdir()

        ensure_initialized(cfg, trust)

        data = json.loads((cfg / ".claude.json").read_text())
        for k, v in original.items():
            assert data[k] == v

    def test_idempotent_no_second_write(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        _write_claude_json(cfg, {})
        trust = tmp_path / "workspace"
        trust.mkdir()

        first = ensure_initialized(cfg, trust)
        mtime1 = (cfg / ".claude.json").stat().st_mtime_ns
        second = ensure_initialized(cfg, trust)
        mtime2 = (cfg / ".claude.json").stat().st_mtime_ns

        assert first is True
        assert second is False
        assert mtime1 == mtime2

    def test_adds_new_trust_dir_without_clobbering_existing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        d1 = tmp_path / "ws1"
        d2 = tmp_path / "ws2"
        d1.mkdir()
        d2.mkdir()
        _write_claude_json(cfg, {})

        ensure_initialized(cfg, d1)
        ensure_initialized(cfg, d2)

        data = json.loads((cfg / ".claude.json").read_text())
        assert data["projects"][str(d1.resolve())]["hasTrustDialogAccepted"] is True
        assert data["projects"][str(d2.resolve())]["hasTrustDialogAccepted"] is True

    def test_resolves_trust_dir_to_canonical_absolute(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this filesystem")
        _write_claude_json(cfg, {})

        ensure_initialized(cfg, link)

        data = json.loads((cfg / ".claude.json").read_text())
        # The canonical path (real, not link) is the projects[] key.
        assert str(real.resolve()) in data["projects"]
        assert str(link) not in data["projects"]


class TestEnsureInitializedNoOps:
    def test_missing_claude_json_is_noop(self, tmp_path: Path) -> None:
        cfg = tmp_path / "fresh-no-claude-json"
        cfg.mkdir()
        trust = tmp_path / "workspace"
        trust.mkdir()

        changed = ensure_initialized(cfg, trust)

        assert changed is False
        assert not (cfg / ".claude.json").exists()

    def test_malformed_json_is_noop(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text("{not-json")
        trust = tmp_path / "workspace"
        trust.mkdir()

        changed = ensure_initialized(cfg, trust)

        assert changed is False
        assert (cfg / ".claude.json").read_text() == "{not-json"

    def test_non_dict_root_is_noop(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text("[1, 2, 3]")
        trust = tmp_path / "workspace"
        trust.mkdir()

        changed = ensure_initialized(cfg, trust)

        assert changed is False

    def test_non_dict_projects_is_noop(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        _write_claude_json(cfg, {"projects": "not-a-dict"})
        trust = tmp_path / "workspace"
        trust.mkdir()

        changed = ensure_initialized(cfg, trust)

        assert changed is False

    def test_non_dict_existing_project_entry_is_noop(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        trust = tmp_path / "workspace"
        trust.mkdir()
        # Existing entry for trust_dir is the wrong shape — bail out
        # rather than clobber.
        _write_claude_json(cfg, {"projects": {str(trust.resolve()): "wrong"}})

        changed = ensure_initialized(cfg, trust)

        assert changed is False


class TestEnsureInitializedConfigDirResolution:
    def test_empty_string_falls_back_to_home_claude(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_home = tmp_path / "home"
        cfg = fake_home / ".claude"
        _write_claude_json(cfg, {})
        monkeypatch.setenv("HOME", str(fake_home))
        # On Linux, Path.home() honors $HOME.
        trust = tmp_path / "workspace"
        trust.mkdir()

        changed = ensure_initialized("", trust)

        assert changed is True
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["hasCompletedOnboarding"] is True

    def test_none_falls_back_to_home_claude(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_home = tmp_path / "home"
        cfg = fake_home / ".claude"
        _write_claude_json(cfg, {})
        monkeypatch.setenv("HOME", str(fake_home))
        trust = tmp_path / "workspace"
        trust.mkdir()

        changed = ensure_initialized(None, trust)

        assert changed is True


class TestEnsureInitializedAtomicity:
    def test_uses_atomic_replace(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        _write_claude_json(cfg, {})
        trust = tmp_path / "workspace"
        trust.mkdir()

        ensure_initialized(cfg, trust)

        # No leftover .tmp file from the atomic rename.
        siblings = sorted(p.name for p in cfg.iterdir())
        assert siblings == [".claude.json"]

    def test_keeps_mode_0600(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        _write_claude_json(cfg, {})
        trust = tmp_path / "workspace"
        trust.mkdir()

        ensure_initialized(cfg, trust)

        mode = (cfg / ".claude.json").stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_write_failure_returns_false_and_cleans_tmp(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = tmp_path / "cfg"
        _write_claude_json(cfg, {})
        trust = tmp_path / "workspace"
        trust.mkdir()
        before_bytes = (cfg / ".claude.json").read_bytes()

        # Simulate disk-failure mid-write. Patch Path.write_text to raise
        # OSError for the tmp file specifically; reads from the original
        # file still succeed.
        from claude_task_runner import claude_init as _ci

        real_write_text = Path.write_text

        def boom(self: Path, *args: object, **kwargs: object) -> int:
            if self.name.endswith(".claude.json.tmp"):
                raise OSError(28, "No space left on device")
            return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "write_text", boom)

        changed = _ci.ensure_initialized(cfg, trust)

        assert changed is False
        # Original file is intact.
        assert (cfg / ".claude.json").read_bytes() == before_bytes
        # No leftover .tmp.
        assert not (cfg / ".claude.json.tmp").exists()

    def test_already_set_does_not_touch_disk(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        trust = tmp_path / "workspace"
        trust.mkdir()
        _write_claude_json(
            cfg,
            {
                "hasCompletedOnboarding": True,
                "projects": {str(trust.resolve()): {"hasTrustDialogAccepted": True}},
            },
        )
        before = (cfg / ".claude.json").stat().st_mtime_ns

        changed = ensure_initialized(cfg, trust)

        after = (cfg / ".claude.json").stat().st_mtime_ns
        assert changed is False
        assert before == after


# Silence pyflakes for the `stat` import (used for symbolic mode constants
# in earlier drafts; left in case future tests need it).
_ = stat.S_IRUSR
