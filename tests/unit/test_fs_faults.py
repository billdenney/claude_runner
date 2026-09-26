"""Tests for tests/unit/_fs_faults.py: the errors it injects are the kernel's.

A real ``PermissionError`` from a read-only or unsearchable directory has
errno EACCES, the path as its filename, and prints as
``[Errno 13] Permission denied: '<path>'``. The tests that use these
helpers pin that text, so it is pinned here too.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from ._fs_faults import read_only, unsearchable


def _assert_denied(exc: PermissionError, path: Path) -> None:
    assert exc.errno == errno.EACCES
    assert exc.filename == str(path)
    assert str(exc) == f"[Errno {errno.EACCES}] Permission denied: '{path}'"


class TestReadOnly:
    def test_refuses_a_new_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        read_only(monkeypatch, tmp_path)
        with pytest.raises(PermissionError) as excinfo:
            (tmp_path / "new").mkdir()
        _assert_denied(excinfo.value, tmp_path / "new")
        assert not (tmp_path / "new").exists()

    def test_refuses_a_new_directory_made_by_makedirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Path.mkdir(parents=True)`` and ``shutil.copytree`` go through ``os.makedirs``."""
        read_only(monkeypatch, tmp_path)
        with pytest.raises(PermissionError) as excinfo:
            os.makedirs(tmp_path / "new" / "deeper")
        _assert_denied(excinfo.value, tmp_path / "new")

    def test_an_existing_name_is_still_there(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "old").mkdir()
        read_only(monkeypatch, tmp_path)
        with pytest.raises(FileExistsError):
            (tmp_path / "old").mkdir()
        (tmp_path / "old").mkdir(exist_ok=True)

    def test_refuses_a_new_symlink(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        read_only(monkeypatch, tmp_path)
        with pytest.raises(PermissionError) as excinfo:
            (tmp_path / "link").symlink_to(tmp_path)
        _assert_denied(excinfo.value, tmp_path / "link")
        assert not (tmp_path / "link").is_symlink()

    def test_refuses_to_unlink_an_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "file").write_text("x", encoding="utf-8")
        read_only(monkeypatch, tmp_path)
        with pytest.raises(PermissionError) as excinfo:
            (tmp_path / "file").unlink()
        _assert_denied(excinfo.value, tmp_path / "file")
        assert (tmp_path / "file").exists()

    def test_unlinking_a_missing_name_is_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        read_only(monkeypatch, tmp_path)
        with pytest.raises(FileNotFoundError):
            (tmp_path / "missing").unlink()

    def test_other_directories_keep_their_own_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only entries directly in the directory are refused, as with a real mode."""
        (tmp_path / "locked" / "sub").mkdir(parents=True)
        read_only(monkeypatch, tmp_path / "locked")
        (tmp_path / "locked" / "sub" / "deeper").mkdir()
        (tmp_path / "sibling").mkdir()
        (tmp_path / "sibling" / "link").symlink_to(tmp_path)
        (tmp_path / "sibling" / "link").unlink()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["locked", "sibling"]


class TestUnsearchable:
    @pytest.mark.parametrize("relative", ["sub", "sub/missing"])
    def test_refuses_to_stat_below(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
    ) -> None:
        """A path below it cannot be looked up, so even a missing one is EACCES."""
        (tmp_path / "locked" / "sub").mkdir(parents=True)
        unsearchable(monkeypatch, tmp_path / "locked")
        path = tmp_path / "locked" / relative
        with pytest.raises(PermissionError) as excinfo:
            path.stat()
        _assert_denied(excinfo.value, path)
        with pytest.raises(PermissionError) as excinfo:
            path.stat(follow_symlinks=False)
        _assert_denied(excinfo.value, path)
        with pytest.raises(PermissionError) as excinfo:
            os.lstat(path)
        _assert_denied(excinfo.value, path)

    def test_the_directory_itself_and_others_still_stat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "locked").mkdir()
        (tmp_path / "other").mkdir()
        unsearchable(monkeypatch, tmp_path / "locked")
        assert (tmp_path / "locked").is_dir()
        assert os.lstat(tmp_path / "locked").st_ino == (tmp_path / "locked").stat().st_ino
        assert (tmp_path / "other").is_dir()
