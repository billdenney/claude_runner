"""Tests for cron.registry — the queues the cron watchdog manages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_task_runner.cron.registry import (
    RegistryError,
    load_registered_queues,
    queues_registry_path,
    read_registered_queues,
    register_queue,
)


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

    def test_register_multiple_queues(self, isolated_home: Path) -> None:
        for name in ("a", "b", "c"):
            (isolated_home / name).mkdir()
            register_queue(isolated_home / name)
        out = load_registered_queues()
        assert len(out) == 3

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
