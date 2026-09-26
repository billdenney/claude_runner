"""Tests for cron.registry — the queues the cron watchdog manages."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
from pathlib import Path

import pytest

from claude_task_runner.cron import registry as registry_mod
from claude_task_runner.cron.registry import (
    RegistryError,
    handover_note,
    ignored_queues,
    load_registered_queues,
    managed_queue,
    queues_registry_path,
    read_registered_queues,
    register_queue,
    unregister_queue,
)
from claude_task_runner.supervisor.pidfile import acquire_global_lock, global_lock_path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` for every test in this file so we never
    touch the real ``~/.claude_task_runner``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestRegistry:
    def test_register_and_load(self, isolated_home: Path) -> None:
        queue = isolated_home / "queue1"
        queue.mkdir()
        register_queue(queue)
        out = load_registered_queues()
        assert out == [queue.resolve()]

    def test_register_idempotent(self, isolated_home: Path) -> None:
        queue = isolated_home / "queue1"
        queue.mkdir()
        register_queue(queue)
        register_queue(queue)
        register_queue(queue)
        assert len(load_registered_queues()) == 1

    def test_register_replaces_the_registered_queue(self, isolated_home: Path) -> None:
        """One supervisor runs per user, so the watchdog manages one queue."""
        a, b, c = (isolated_home / name for name in ("a", "b", "c"))
        for q in (a, b, c):
            q.mkdir()
        assert register_queue(a) == []
        assert register_queue(b) == [a.resolve()]
        assert register_queue(c) == [b.resolve()]
        assert load_registered_queues() == [c.resolve()]

    def test_reregistering_the_only_queue_writes_nothing(self, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        path = queues_registry_path()
        path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        assert register_queue(queue) == []
        assert path.read_text(encoding="utf-8") == before

    def test_register_collapses_an_older_list(self, isolated_home: Path) -> None:
        """An older version appended, so the file may list several queues."""
        a, b, c = (isolated_home / name for name in ("a", "b", "c"))
        for q in (a, b, c):
            q.mkdir()
        _write_registry_file([str(a), str(b), str(a)])
        assert register_queue(c) == [a, b]
        assert read_registered_queues() == [c.resolve()]

    def test_register_collapses_an_older_list_that_ends_with_the_queue(
        self, isolated_home: Path
    ) -> None:
        a, b = isolated_home / "a", isolated_home / "b"
        a.mkdir()
        b.mkdir()
        _write_registry_file([str(a), str(b)])
        assert register_queue(b) == [a]
        assert read_registered_queues() == [b.resolve()]

    def test_load_missing_returns_empty(self, isolated_home: Path) -> None:
        assert load_registered_queues() == []

    def test_load_corrupt_returns_empty(self, isolated_home: Path) -> None:
        path = queues_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert load_registered_queues() == []

    def test_register_rejects_missing_directory(self, isolated_home: Path) -> None:
        """A tick would create a registered-but-missing queue dir on restart."""
        missing = isolated_home / "no-such-queue"
        with pytest.raises(NotADirectoryError, match="not an existing directory"):
            register_queue(missing)
        assert not queues_registry_path().exists()
        assert not missing.exists()

    def test_register_rejects_a_file(self, isolated_home: Path) -> None:
        not_a_dir = isolated_home / "queue.txt"
        not_a_dir.write_text("", encoding="utf-8")
        with pytest.raises(NotADirectoryError, match="not an existing directory"):
            register_queue(not_a_dir)
        assert not queues_registry_path().exists()

    def test_rejected_register_keeps_existing_entries(self, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        with pytest.raises(NotADirectoryError):
            register_queue(isolated_home / "typo")
        assert load_registered_queues() == [queue.resolve()]


class TestCorruptRegistryBackup:
    """Audit finding 2: a corrupt registry must be logged + backed up,
    not silently reset to empty."""

    def test_corrupt_json_logs_and_backs_up(
        self,
        isolated_home: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = queues_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        with caplog.at_level("ERROR", logger="claude_task_runner.cron.registry"):
            out = load_registered_queues()

        assert out == []
        # A .broken backup must be written alongside the original.
        backup = path.with_suffix(path.suffix + ".broken")
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == "{not json"
        # And the failure must be logged at ERROR with the path.
        assert any(
            record.levelname == "ERROR" and str(path) in record.getMessage()
            for record in caplog.records
        ), caplog.text

    def test_non_object_payload_logs_and_backs_up(
        self,
        isolated_home: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A syntactically-valid JSON that isn't an object (e.g. a list)
        is also corruption — same treatment."""
        path = queues_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('["not", "a", "dict"]', encoding="utf-8")

        with caplog.at_level("ERROR", logger="claude_task_runner.cron.registry"):
            out = load_registered_queues()

        assert out == []
        backup = path.with_suffix(path.suffix + ".broken")
        assert backup.exists()
        assert any(record.levelname == "ERROR" for record in caplog.records), caplog.text

    def test_valid_registry_not_backed_up(self, isolated_home: Path) -> None:
        """A well-formed registry must NOT trigger a .broken backup."""
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        backup = queues_registry_path().with_suffix(queues_registry_path().suffix + ".broken")
        assert load_registered_queues() == [queue.resolve()]
        assert not backup.exists()


CORRUPT_PAYLOADS = {
    "not-json": "{not json",
    "not-an-object": '["not", "a", "dict"]',
    "queues-not-a-list": '{"queues": "/one/queue"}',
}
"""Every way a readable ``queues.json`` can fail to hold a registry."""


class TestReadRegisteredQueues:
    """The strict reader tells an empty registry from a broken one and writes nothing."""

    def test_missing_file_is_empty(self) -> None:
        assert read_registered_queues() == []
        assert not queues_registry_path().exists()

    def test_valid_registry(self, isolated_home: Path) -> None:
        queues = [isolated_home / "a", isolated_home / "b"]
        path = queues_registry_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"queues": [str(q) for q in queues]}), encoding="utf-8")
        assert read_registered_queues() == queues

    def test_object_without_queues_key_is_empty(self) -> None:
        path = queues_registry_path()
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        assert read_registered_queues() == []

    @pytest.mark.parametrize("payload", CORRUPT_PAYLOADS.values(), ids=CORRUPT_PAYLOADS.keys())
    def test_corrupt_registry_raises_and_writes_nothing(self, payload: str) -> None:
        path = queues_registry_path()
        path.parent.mkdir(parents=True)
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(RegistryError, match=str(path)):
            read_registered_queues()
        assert path.read_text(encoding="utf-8") == payload
        assert sorted(p.name for p in path.parent.iterdir()) == ["queues.json"]

    @pytest.mark.parametrize("payload", CORRUPT_PAYLOADS.values(), ids=CORRUPT_PAYLOADS.keys())
    def test_lenient_loader_backs_up_every_corrupt_form(
        self, payload: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The same inputs through the tick's reader: logged, kept, treated as empty."""
        path = queues_registry_path()
        path.parent.mkdir(parents=True)
        path.write_text(payload, encoding="utf-8")
        with caplog.at_level("ERROR", logger="claude_task_runner.cron.registry"):
            assert load_registered_queues() == []
        backup = path.with_suffix(path.suffix + ".broken")
        assert backup.read_text(encoding="utf-8") == payload
        assert any(str(path) in r.getMessage() for r in caplog.records), caplog.text

    def test_unreadable_registry_raises(self) -> None:
        """A directory where the file should be: the read fails with an OSError."""
        path = queues_registry_path()
        path.mkdir(parents=True)
        with pytest.raises(RegistryError, match="corrupt queues registry"):
            read_registered_queues()

    def test_lenient_loader_survives_a_failed_backup(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = queues_registry_path()
        path.mkdir(parents=True)
        with caplog.at_level("ERROR", logger="claude_task_runner.cron.registry"):
            assert load_registered_queues() == []
        assert "could not back up corrupt registry" in caplog.text


def _write_registry_file(entries: list[str]) -> Path:
    """Write ``queues.json`` by hand, the way an operator or an old version might have."""
    path = queues_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"queues": entries}), encoding="utf-8")
    return path


class TestManagedQueue:
    """The last entry is managed; an older list's others are ignored."""

    @pytest.mark.parametrize(
        ("entries", "managed", "ignored"),
        [
            pytest.param([], None, [], id="empty"),
            pytest.param(["/q/a"], "/q/a", [], id="one"),
            pytest.param(["/q/a", "/q/b"], "/q/b", ["/q/a"], id="older-list"),
            pytest.param(["/q/b", "/q/b"], "/q/b", [], id="duplicate-of-the-managed"),
            pytest.param(["/q/a", "/q/b", "/q/a"], "/q/a", ["/q/b"], id="managed-listed-twice"),
            pytest.param(
                ["/q/a", "/q/c", "/q/b", "/q/c", "/q/a", "/q/d"],
                "/q/d",
                ["/q/a", "/q/c", "/q/b"],
                id="each-ignored-once-in-order",
            ),
        ],
    )
    def test_managed_and_ignored(
        self, entries: list[str], managed: str | None, ignored: list[str]
    ) -> None:
        queues = [Path(e) for e in entries]
        assert managed_queue(queues) == (Path(managed) if managed is not None else None)
        assert ignored_queues(queues) == [Path(i) for i in ignored]


class TestUnregisterQueue:
    def test_removes_only_that_queue(self, isolated_home: Path) -> None:
        """In a list an older version wrote, the other entries stay."""
        queues = [isolated_home / name for name in ("a", "b", "c")]
        _write_registry_file([str(q) for q in queues])
        assert unregister_queue(queues[1]) == [queues[1]]
        assert read_registered_queues() == [queues[0], queues[2]]

    def test_second_call_is_a_no_op(self, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        assert unregister_queue(queue) == [queue.resolve()]
        before = queues_registry_path().read_text(encoding="utf-8")
        assert unregister_queue(queue) == []
        assert queues_registry_path().read_text(encoding="utf-8") == before
        assert load_registered_queues() == []

    def test_without_a_registry_writes_nothing(self, isolated_home: Path) -> None:
        assert unregister_queue(isolated_home / "q") == []
        assert not queues_registry_path().parent.exists()

    def test_queue_that_no_longer_exists(self, isolated_home: Path) -> None:
        """The main use: drop a queue that was deleted after it was registered."""
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        queue.rmdir()
        assert unregister_queue(queue) == [queue.resolve()]
        assert load_registered_queues() == []
        assert not queue.exists()

    def test_relative_path_is_made_absolute(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        monkeypatch.chdir(isolated_home)
        assert unregister_queue(Path("q")) == [queue.resolve()]
        assert load_registered_queues() == []

    def test_entry_whose_symlink_changed_matches_as_written(self, isolated_home: Path) -> None:
        """The entry no longer resolves to itself, yet the path the operator
        copies from ``watchdog queues`` must still remove it."""
        real = isolated_home / "real"
        (real / "q").mkdir(parents=True)
        link = isolated_home / "link"
        link.symlink_to(real, target_is_directory=True)
        entry = link / "q"
        _write_registry_file([str(entry)])
        assert entry.resolve() != entry
        assert unregister_queue(entry) == [entry]
        assert read_registered_queues() == []

    def test_resolved_form_matches_a_symlinked_argument(self, isolated_home: Path) -> None:
        real = isolated_home / "real"
        (real / "q").mkdir(parents=True)
        link = isolated_home / "link"
        link.symlink_to(real, target_is_directory=True)
        register_queue(real / "q")
        assert unregister_queue(link / "q") == [(real / "q").resolve()]
        assert read_registered_queues() == []

    def test_duplicate_entries_all_go(self, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        other = isolated_home / "other"
        _write_registry_file([str(queue), str(other), str(queue)])
        assert unregister_queue(queue) == [queue, queue]
        assert read_registered_queues() == [other]

    @pytest.mark.parametrize("payload", CORRUPT_PAYLOADS.values(), ids=CORRUPT_PAYLOADS.keys())
    def test_corrupt_registry_raises_and_is_left_alone(self, payload: str) -> None:
        """The lenient reader would call it empty, and rewriting that drops every queue."""
        path = queues_registry_path()
        path.parent.mkdir(parents=True)
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(RegistryError, match=re.escape(str(path))):
            unregister_queue(Path("/some/queue"))
        assert path.read_text(encoding="utf-8") == payload
        assert sorted(p.name for p in path.parent.iterdir()) == ["queues.json"]


class TestRegistryWrites:
    """``register_queue`` and ``unregister_queue`` share one writer."""

    def test_file_format(self, isolated_home: Path) -> None:
        a, b = isolated_home / "a", isolated_home / "b"
        a.mkdir()
        b.mkdir()

        def written(q: Path) -> str:
            return json.dumps({"queues": [str(q.resolve())]}, indent=2) + "\n"

        register_queue(a)
        assert queues_registry_path().read_text(encoding="utf-8") == written(a)
        _write_registry_file([str(a.resolve()), str(b.resolve())])
        unregister_queue(a)
        assert queues_registry_path().read_text(encoding="utf-8") == written(b)

    def test_no_temporary_file_is_left_behind(self, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        unregister_queue(queue)
        assert sorted(p.name for p in queues_registry_path().parent.iterdir()) == ["queues.json"]

    def test_failed_write_changes_nothing(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full disk mid-write keeps the old registry and removes the temporary file."""
        keep = isolated_home / "keep"
        keep.mkdir()
        register_queue(keep)
        before = queues_registry_path().read_text(encoding="utf-8")
        new = isolated_home / "new"
        new.mkdir()

        def _disk_full(_fd: int) -> None:
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(registry_mod.os, "fsync", _disk_full)
        with pytest.raises(OSError, match="No space left on device"):
            register_queue(new)
        assert queues_registry_path().read_text(encoding="utf-8") == before
        assert sorted(p.name for p in queues_registry_path().parent.iterdir()) == ["queues.json"]

    def test_unwritable_registry_directory_changes_nothing(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The temporary file cannot even be created, so there is nothing to clean up."""
        keep = isolated_home / "keep"
        keep.mkdir()
        register_queue(keep)
        before = queues_registry_path().read_text(encoding="utf-8")

        def _denied(*_args: object, **_kwargs: object) -> None:
            raise PermissionError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(registry_mod.tempfile, "NamedTemporaryFile", _denied)
        with pytest.raises(PermissionError, match="Permission denied"):
            unregister_queue(keep)
        assert queues_registry_path().read_text(encoding="utf-8") == before
        assert sorted(p.name for p in queues_registry_path().parent.iterdir()) == ["queues.json"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root is not denied by directory permissions")
def test_register_rejects_a_queue_behind_an_unsearchable_directory(isolated_home: Path) -> None:
    """Refused like a missing directory, as the tick would skip it, not a PermissionError."""
    locked = isolated_home / "locked"
    queue = locked / "q"
    queue.mkdir(parents=True)
    locked.chmod(0o000)
    try:
        with pytest.raises(NotADirectoryError) as excinfo:
            register_queue(queue)
    finally:
        locked.chmod(0o700)
    assert str(excinfo.value) == f"not an existing directory: {queue.resolve()}"
    assert not queues_registry_path().exists()


def _supervisor_pid_file(queue: Path, pid: int) -> None:
    (queue / ".claude_task_runner").mkdir(parents=True, exist_ok=True)
    (queue / ".claude_task_runner" / "supervisor.pid").write_text(f"{pid}\n", encoding="utf-8")


class TestHandoverNote:
    """What ``install`` and ``watchdog register`` say after replacing a queue.

    The lock is held in this process, through a separate open file, so the
    PID it records is this test's."""

    def test_free_lock_needs_no_note(self, isolated_home: Path) -> None:
        old, new = isolated_home / "old", isolated_home / "new"
        _supervisor_pid_file(old, os.getpid())
        assert handover_note(new, [old]) is None

    def test_lock_held_by_this_queues_own_supervisor(self, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        _supervisor_pid_file(queue, os.getpid())
        with acquire_global_lock():
            assert handover_note(queue, []) is None

    def test_lock_held_by_the_replaced_queues_supervisor(self, isolated_home: Path) -> None:
        old, other, new = isolated_home / "old", isolated_home / "other", isolated_home / "new"
        _supervisor_pid_file(other, 1)
        _supervisor_pid_file(old, os.getpid())
        with acquire_global_lock():
            note = handover_note(new, [other, old])
        assert note == (
            f"The supervisor for {old} (pid {os.getpid()}) still holds global.lock, so the "
            "watchdog starts this queue's supervisor once it exits. To hand over now, run: "
            f"claude-task-runner supervisor drain --queue {old}"
        )

    def test_lock_held_by_a_supervisor_of_no_listed_queue(self, isolated_home: Path) -> None:
        """Say a hand-started supervisor for another queue holds it."""
        old, new = isolated_home / "old", isolated_home / "new"
        _supervisor_pid_file(old, 1)
        with acquire_global_lock():
            note = handover_note(new, [old])
        assert note == (
            f"Another supervisor (pid {os.getpid()}) holds global.lock, so the watchdog "
            "starts this queue's supervisor once it exits."
        )

    def test_lock_held_before_its_pid_is_written(self, isolated_home: Path) -> None:
        path = global_lock_path()
        path.write_text("", encoding="utf-8")
        with path.open("a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            note = handover_note(isolated_home / "new", [])
        assert note == (
            "Another supervisor holds global.lock, so the watchdog starts this queue's "
            "supervisor once it exits."
        )

    def test_probe_failure_is_reported(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _denied() -> None:
            raise PermissionError(errno.EACCES, "Permission denied", str(global_lock_path()))

        monkeypatch.setattr(registry_mod.pidfile_mod, "probe_global_lock", _denied)
        assert handover_note(isolated_home / "new", []) == (
            "Could not check whether another supervisor holds global.lock: "
            f"[Errno 13] Permission denied: '{global_lock_path()}'"
        )
