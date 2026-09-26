"""Tests for queue/store.py — atomic YAML I/O for tasks and state."""

from __future__ import annotations

import codecs
import errno
import io
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from claude_task_runner.queue.schema import (
    CURRENT_SCHEMA_VERSION,
    ReadinessRequirement,
    RunRecord,
    Task,
    TaskState,
    TokenUsage,
)
from claude_task_runner.queue.store import (
    MAX_YAML_BYTES,
    MAX_YAML_DEPTH,
    QueueIOError,
    QueueSchemaError,
    _load_yaml,
    _yaml_loader,
    list_pending_tasks,
    list_state_files,
    load_state,
    load_task,
    queue_runtime_dir,
    require_queue_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)

QUEUE_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "queue"

FIXTURE_LOADERS: dict[str, Callable[[Path], BaseModel]] = {
    "tasks/minimal.yaml": load_task,
    "tasks/hand_authored.yaml": load_task,
    "tasks/runner_written.yaml": load_task,
    "states/pending_minimal.yaml": load_state,
    "states/hand_edited.yaml": load_state,
    "states/runner_written.yaml": load_state,
}
"""Every file under ``tests/fixtures/queue/`` and the loader it is read with."""

BYTE_VARIANTS: dict[str, Callable[[bytes], bytes]] = {
    "as-is": lambda b: b,
    "crlf": lambda b: b.replace(b"\n", b"\r\n"),
    "utf8-bom": lambda b: codecs.BOM_UTF8 + b,
    "utf16le-bom": lambda b: codecs.BOM_UTF16_LE + b.decode("utf-8").encode("utf-16-le"),
}
"""Forms an operator's editor may save a queue YAML in. Both loaders read
the file as bytes and detect the encoding themselves."""

requires_libyaml = pytest.mark.skipif(
    not yaml.__with_libyaml__, reason="PyYAML was built without LibYAML"
)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "myqueue"
    qd.mkdir()
    return qd


@pytest.fixture
def loaders_used(monkeypatch: pytest.MonkeyPatch) -> list[type]:
    """Record the ``Loader`` class of every ``yaml.load`` call."""
    used: list[type] = []
    real_load = yaml.load

    def spy(stream: Any, Loader: type) -> Any:
        used.append(Loader)
        return real_load(stream, Loader=Loader)

    monkeypatch.setattr(yaml, "load", spy)
    return used


_CHILD_LOAD_TASK = """
import sys
from pathlib import Path

if sys.argv[2] == "pure-python":
    # What a PyYAML built without LibYAML looks like: yaml imports, but
    # yaml.cyaml cannot, so there is no CSafeLoader at all.
    sys.modules["yaml._yaml"] = None

import yaml

assert yaml.__with_libyaml__ is (sys.argv[2] == "libyaml"), yaml.__with_libyaml__

from claude_task_runner.queue.store import QueueSchemaError, load_task

try:
    print(load_task(Path(sys.argv[1])).model_dump_json())
except QueueSchemaError as exc:
    print(str(exc).splitlines()[-1])
"""


def _load_task_in_child(path: Path, backend: str) -> str:
    """``load_task(path)`` in a fresh interpreter whose PyYAML looks the
    way ``backend`` names, so even an import-time failure is caught, and a
    crash fails the test instead of killing pytest. Returns the Task as
    JSON, or the last line of the QueueSchemaError."""
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_LOAD_TASK, str(path), backend],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"child exited {proc.returncode}: {proc.stderr[-2000:]}"
    return proc.stdout


def _task_nested(depth: int) -> str:
    """A task YAML whose deepest node sits exactly ``depth`` levels down.

    The root mapping is level 1, the ``tags`` list is level 2, each further
    ``[`` adds a level, and the ``x`` scalar is the deepest node.
    """
    brackets = depth - 2
    return f"id: x\ntitle: T\nprompt: P\ntags: {'[' * brackets}x{']' * brackets}\n"


class TestRuntimeDir:
    def test_creates_runtime_subdirs(self, queue_dir: Path) -> None:
        runtime = queue_runtime_dir(queue_dir)
        assert runtime.is_dir()
        assert (runtime / "state").is_dir()
        assert (runtime / "sidecar").is_dir()
        assert (runtime / "logs").is_dir()

    def test_idempotent(self, queue_dir: Path) -> None:
        first = queue_runtime_dir(queue_dir)
        second = queue_runtime_dir(queue_dir)
        assert first == second


class TestRoundTrip:
    @pytest.fixture(autouse=True)
    def _each_loader(self, yaml_backend: str) -> None:
        """Whatever the runner writes must read back under either loader."""

    def test_task_round_trip(self, queue_dir: Path) -> None:
        t = Task(
            id="001-fiedler",
            title="Extract Fiedler 2019 fremanezumab",
            prompt="...",
            allowed_tools=["Read", "Write"],
            tags=["paper", "popPK"],
            effort="high",
        )
        path = task_path_for(queue_dir, t.id)
        write_task_atomic(t, path)
        loaded = load_task(path)
        assert loaded == t

    def test_state_round_trip(self, queue_dir: Path) -> None:
        when = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        run = RunRecord(
            attempt=1,
            started_at=when,
            finished_at=when,
            stop_reason="end_turn",
            duration_s=1.5,
            usage=TokenUsage(input_tokens=10, output_tokens=20),
        )
        s = TaskState(
            task_id="001",
            status="completed",
            attempts=1,
            session_id="sess-abc",
            last_started_at=when,
            last_finished_at=when,
            stop_reason="end_turn",
            runs=[run],
        )
        path = state_path_for(queue_dir, s.task_id)
        write_state_atomic(s, path)
        loaded = load_state(path)
        assert loaded == s
        assert loaded.runs[0].usage.total_tokens == 30


class TestAtomicity:
    def test_write_uses_replace_not_truncate(self, queue_dir: Path) -> None:
        """Concurrent reads should never see a partial file. We simulate
        by writing twice and verifying the file is always loadable as
        a complete TaskState."""
        path = state_path_for(queue_dir, "001")
        # First write
        write_state_atomic(TaskState(task_id="001", status="pending"), path)
        first = load_state(path)
        # Second write with mutation
        write_state_atomic(TaskState(task_id="001", status="running", attempts=1), path)
        second = load_state(path)
        assert first.status == "pending"
        assert second.status == "running"

    def test_no_tmp_files_left_behind(self, queue_dir: Path) -> None:
        path = state_path_for(queue_dir, "001")
        write_state_atomic(TaskState(task_id="001"), path)
        leftovers = list(path.parent.glob(".*tmp*"))
        assert leftovers == []


class TestSchemaVersionGuard:
    @pytest.fixture(autouse=True)
    def _each_loader(self, yaml_backend: str) -> None:
        """The guard must hold under either loader."""

    def test_unversioned_yaml_uses_default(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text("id: x\ntitle: T\nprompt: P\n")
        loaded = load_task(path)
        assert loaded.schema_version == CURRENT_SCHEMA_VERSION

    def test_wrong_schema_version_rejected(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text("schema_version: 99\nid: x\ntitle: T\nprompt: P\n")
        with pytest.raises(QueueSchemaError, match="schema_version=99"):
            load_task(path)


class TestErrorHandling:
    @pytest.fixture(autouse=True)
    def _each_loader(self, yaml_backend: str) -> None:
        """Every loader error must still surface as a QueueSchemaError."""

    def test_invalid_yaml_raises(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text(":\n: : :\n")
        with pytest.raises(QueueSchemaError, match="invalid YAML"):
            load_task(path)

    def test_non_mapping_yaml_rejected(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(QueueSchemaError, match="mapping"):
            load_task(path)

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            (b"", "top-level YAML must be a mapping"),
            (b"# only a comment\n", "top-level YAML must be a mapping"),
            (b"just a scalar\n", "top-level YAML must be a mapping"),
            (b"id: a\n---\nid: b\n", "invalid YAML: expected a single document"),
            (b"id: caf\xe9\n", "invalid YAML"),
            (b"id: x\x00y\n", "invalid YAML"),
        ],
        ids=["empty", "comment-only", "scalar", "two-documents", "not-utf8", "nul-byte"],
    )
    def test_degenerate_input_is_a_schema_error(
        self, queue_dir: Path, content: bytes, message: str
    ) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_bytes(content)
        with pytest.raises(QueueSchemaError, match=message):
            load_task(path)

    def test_missing_file_is_an_io_error(self, queue_dir: Path) -> None:
        with pytest.raises(QueueIOError, match="failed to read"):
            load_task(task_path_for(queue_dir, "absent"))

    def test_read_failure_is_an_io_error_not_a_schema_error(
        self, queue_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``reconcile_corrupt`` quarantines a state file on a schema error
        but skips an I/O error as transient, so an EIO raised while the
        loader reads (LibYAML reads through a C callback) must still
        surface as a QueueIOError."""
        path = task_path_for(queue_dir, "x")
        path.write_text("id: x\ntitle: T\nprompt: P\n")

        class FailingRead(io.RawIOBase):
            def readable(self) -> bool:
                return True

            def readinto(self, buffer: Any) -> int:
                raise OSError(errno.EIO, "Input/output error")

        real_open = Path.open

        def open_failing(self: Path, *args: Any, **kwargs: Any) -> Any:
            return FailingRead() if self == path else real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_failing)
        with pytest.raises(QueueIOError, match="Input/output error"):
            load_task(path)

    def test_validation_failure_surfaces(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text("id: x\ntitle: T\nprompt: P\npriority: urgent\n")
        with pytest.raises(QueueSchemaError):
            load_task(path)

    def test_write_to_missing_dir_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "nope" / "x.yaml"
        with pytest.raises(QueueIOError, match="parent dir"):
            write_state_atomic(TaskState(task_id="x"), path)

    def test_oversized_yaml_rejected_before_parse(self, queue_dir: Path) -> None:
        """A pathological YAML larger than the size limit is rejected on
        stat, before the YAML parser can expand it and stall a tick."""
        path = task_path_for(queue_dir, "x")
        # Valid YAML mapping, but padded with a comment past the limit so
        # the rejection is purely size-driven (not a parse/validation fail).
        padding = "#" + ("y" * (MAX_YAML_BYTES + 1))
        path.write_text(f"id: x\ntitle: T\nprompt: P\n{padding}\n")
        assert path.stat().st_size > MAX_YAML_BYTES
        with pytest.raises(QueueSchemaError, match="exceeds limit"):
            load_task(path)

    def test_at_limit_yaml_loads(self, queue_dir: Path) -> None:
        """A file at exactly the limit is accepted — the guard rejects
        only strictly-larger files."""
        path = task_path_for(queue_dir, "x")
        body = "id: x\ntitle: T\nprompt: P\n"
        # body + "#" (1) + pad_len "y"s + "\n" (1) == MAX_YAML_BYTES
        pad_len = MAX_YAML_BYTES - len(body.encode()) - 2
        path.write_text(f"{body}#{'y' * pad_len}\n")
        assert path.stat().st_size == MAX_YAML_BYTES
        loaded = load_task(path)
        assert loaded.id == "x"


class TestListing:
    def test_list_pending_returns_sorted(self, queue_dir: Path) -> None:
        td = todo_dir(queue_dir)
        for tid in ("003-c", "001-a", "002-b"):
            (td / f"{tid}.yaml").write_text(f"id: {tid}\ntitle: T\nprompt: P\n")
        names = [p.stem for p in list_pending_tasks(queue_dir)]
        assert names == ["001-a", "002-b", "003-c"]

    def test_list_states(self, queue_dir: Path) -> None:
        for tid in ("001-a", "002-b"):
            write_state_atomic(
                TaskState(task_id=tid),
                state_path_for(queue_dir, tid),
            )
        names = [p.stem for p in list_state_files(queue_dir)]
        assert names == ["001-a", "002-b"]


class TestLoaderSelection:
    @requires_libyaml
    def test_parses_with_libyaml_when_available(
        self, queue_dir: Path, loaders_used: list[type]
    ) -> None:
        """The speedup depends on this. A change that quietly fell back to
        the pure-Python loader would pass every other test."""
        path = task_path_for(queue_dir, "x")
        path.write_text("id: x\ntitle: T\nprompt: P\n")
        load_task(path)
        assert len(loaders_used) == 1
        assert issubclass(loaders_used[0], yaml.CSafeLoader)

    def test_falls_back_to_safe_loader_without_libyaml(
        self, monkeypatch: pytest.MonkeyPatch, queue_dir: Path, loaders_used: list[type]
    ) -> None:
        c_loader = getattr(yaml, "CSafeLoader", None)
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
        path = task_path_for(queue_dir, "x")
        path.write_text("id: x\ntitle: T\nprompt: P\n")
        assert load_task(path) == Task(id="x", title="T", prompt="P")
        assert len(loaders_used) == 1
        assert issubclass(loaders_used[0], yaml.SafeLoader)
        assert c_loader is None or not issubclass(loaders_used[0], c_loader)
        # Errors still translate on the fallback path.
        path.write_text(":\n: : :\n")
        with pytest.raises(QueueSchemaError, match="invalid YAML"):
            load_task(path)

    def test_imports_and_loads_without_libyaml(self) -> None:
        """Deleting ``yaml.CSafeLoader`` after import (the fallback test
        above) cannot catch a module that fails to IMPORT on such a build,
        so a fresh interpreter imports PyYAML the way a LibYAML-less build
        has it and must read the fullest fixture exactly as this one does."""
        path = QUEUE_FIXTURES / "tasks" / "runner_written.yaml"
        assert _load_task_in_child(path, "pure-python") == load_task(path).model_dump_json() + "\n"

    def test_loader_class_is_built_once_per_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``_yaml_loader()`` runs once per file, so the depth-limited class
        must be built once and reused, not rebuilt per call."""
        preferred = _yaml_loader()
        assert _yaml_loader() is preferred
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
        fallback = _yaml_loader()
        assert _yaml_loader() is fallback
        assert issubclass(fallback, yaml.SafeLoader)


class TestLoaderParity:
    """LibYAML's ``CSafeLoader`` and the pure-Python ``SafeLoader`` must
    build the same Task / TaskState from a queue file, or switching loaders
    would change what the runner dispatches."""

    def test_fixture_registry_matches_disk(self) -> None:
        """A fixture added on disk but not to FIXTURE_LOADERS would go
        untested, and an empty glob would make the parity test vacuous."""
        on_disk = sorted(
            p.relative_to(QUEUE_FIXTURES).as_posix() for p in QUEUE_FIXTURES.glob("*/*.yaml")
        )
        assert on_disk == sorted(FIXTURE_LOADERS)

    @requires_libyaml
    @pytest.mark.parametrize("variant", sorted(BYTE_VARIANTS))
    @pytest.mark.parametrize("name", sorted(FIXTURE_LOADERS))
    def test_both_loaders_build_identical_objects(
        self, name: str, variant: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        load = FIXTURE_LOADERS[name]
        path = tmp_path / Path(name).name
        path.write_bytes(BYTE_VARIANTS[variant]((QUEUE_FIXTURES / name).read_bytes()))

        c_loader = yaml.CSafeLoader
        assert issubclass(_yaml_loader(), c_loader)
        c_raw, c_obj = _load_yaml(path), load(path)
        monkeypatch.delattr(yaml, "CSafeLoader")
        assert not issubclass(_yaml_loader(), c_loader)
        py_raw, py_obj = _load_yaml(path), load(path)

        # repr() is strict about types and key order where == is not
        # (1 == 1.0 == True), so a scalar resolved differently cannot hide.
        assert repr(c_raw) == repr(py_raw)
        assert c_obj == py_obj
        # JSON keeps each datetime's UTC offset, which == on aware
        # datetimes ignores.
        assert c_obj.model_dump_json() == py_obj.model_dump_json()


class TestQueueFixtures:
    """Known answers for the parity fixtures. A fixture that quietly stopped
    exercising what it claims to would let the parity test pass vacuously."""

    @pytest.fixture(autouse=True)
    def _each_loader(self, yaml_backend: str) -> None:
        """Each loader must produce these answers on its own."""

    def test_runner_written_fixtures_populate_every_field(self) -> None:
        """Enumerates the schema: a new field fails here until the
        runner-written fixtures carry it, so the parity test covers it."""
        task = _load_yaml(QUEUE_FIXTURES / "tasks" / "runner_written.yaml")
        assert set(task) == set(Task.model_fields)
        assert [set(r) for r in task["requires"]] == [set(ReadinessRequirement.model_fields)] * 2
        state = _load_yaml(QUEUE_FIXTURES / "states" / "runner_written.yaml")
        assert set(state) == set(TaskState.model_fields)
        assert [set(run) for run in state["runs"]] == [set(RunRecord.model_fields)] * 2
        assert [set(run["usage"]) for run in state["runs"]] == [set(TokenUsage.model_fields)] * 2

    def test_runner_written_task_decodes_escapes(self) -> None:
        """``safe_dump`` escapes non-ASCII text, so this fixture drives the
        scanners' ``\\x`` / ``\\u`` / ``\\U`` escape decoding."""
        path = QUEUE_FIXTURES / "tasks" / "runner_written.yaml"
        text = path.read_text(encoding="utf-8")
        for escape in ("\\xE9", "\\u4E2D", "\\U0001F600", "\\t"):
            assert escape in text
        assert "Ménière, 中文, 😀, and a tab\there." in load_task(path).prompt

    def test_hand_authored_task_resolves_yaml_1_1_scalars(self) -> None:
        raw = _load_yaml(QUEUE_FIXTURES / "tasks" / "hand_authored.yaml")
        assert raw["title"] == "It's hand-written: with a colon"
        assert raw["weekly_critical"] is True
        assert raw["weekly_deferrable"] is False
        assert raw["account"] is None
        assert raw["tags"] == ["hand-written", "on"]
        assert type(raw["max_tokens_override"]) is int
        assert raw["max_tokens_override"] == 1_500_000
        assert raw["max_duration_s_override"] == 3600.5
        assert raw["additional_dirs"] == [raw["working_dir"]]
        assert raw["requires"] == [
            {"kind": "file", "path": "inputs/paper.pdf", "note": "lead PDF, folded onto one line"},
            {
                "kind": "file",
                "path": "inputs/supplement.pdf",
                "note": "supplement (merge key; path and note overridden)",
            },
            {"kind": "sidecar_response"},
        ]
        assert raw["prompt"].startswith("# Goal\n\nExtract the model from `inputs/paper.pdf`.\n")
        assert "\n  - a more-indented line, kept verbatim" in raw["prompt"]
        assert raw["prompt"].endswith("Non-ASCII stays raw: Ménière, 中文, 😀.\n")

    def test_hand_edited_state_resolves_unquoted_timestamps(self) -> None:
        raw = _load_yaml(QUEUE_FIXTURES / "states" / "hand_edited.yaml")
        assert raw["last_started_at"] == datetime(2026, 9, 24, 21, 5, 1, 500_000, tzinfo=UTC)
        assert raw["last_finished_at"] == datetime(2026, 9, 24, 21, 7, 22, 828_054, tzinfo=UTC)
        assert raw["last_heartbeat_at"] == datetime(2026, 9, 24, 21, 7, 22, tzinfo=UTC)
        ist = timezone(timedelta(hours=5, minutes=30))
        assert raw["next_eligible_at"] == datetime(2026, 9, 25, 9, 0, tzinfo=ist)
        assert raw["next_eligible_at"].utcoffset() == timedelta(hours=5, minutes=30)
        assert (
            raw["deferred_reason"] == "pre-dispatch hook deferred: supplement still downloading\n"
        )
        assert raw["error"] == "first line\nsecond line\n\n"
        run = raw["runs"][0]
        assert run["usage"] == {"input_tokens": 1000, "output_tokens": 20, "cache_read_tokens": 0}
        assert (run["cost_usd"], run["duration_s"]) == (0.5, 141.28)


class TestDepthLimit:
    @pytest.fixture(autouse=True)
    def _each_loader(self, yaml_backend: str) -> None:
        """The limit must hold under either loader."""

    def test_nesting_at_the_limit_parses(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text(_task_nested(MAX_YAML_DEPTH))
        node: Any = _load_yaml(path)
        levels = 1
        while not isinstance(node, str):
            node = node["tags"] if isinstance(node, dict) else node[0]
            levels += 1
        assert (node, levels) == ("x", MAX_YAML_DEPTH)

    def test_nesting_past_the_limit_is_a_schema_error(self, queue_dir: Path) -> None:
        path = task_path_for(queue_dir, "x")
        path.write_text(_task_nested(MAX_YAML_DEPTH + 1))
        with pytest.raises(QueueSchemaError) as excinfo:
            load_task(path)
        lines = str(excinfo.value).splitlines()
        assert lines[0] == f"{path}: invalid YAML: while composing a collection"
        # The error points at the level-64 list holding the too-deep node:
        # the 63rd "[" after "tags: ", i.e. 0-based column 6 + 62.
        assert lines[1].startswith(f'  in "{path}", line 4, column 69')
        assert lines[-1] == (
            f"found a node nested more than {MAX_YAML_DEPTH} levels deep (MAX_YAML_DEPTH)"
        )

    def test_wide_documents_are_not_deep(self, queue_dir: Path) -> None:
        """Depth counts nesting, not nodes: many siblings stay shallow."""
        tags = [f"t{i}" for i in range(10 * MAX_YAML_DEPTH)]
        path = task_path_for(queue_dir, "x")
        write_task_atomic(Task(id="x", title="T", prompt="P", tags=tags), path)
        assert load_task(path).tags == tags

    def test_pathological_nesting_is_a_schema_error_not_a_crash(
        self, yaml_backend: str, tmp_path: Path
    ) -> None:
        """Unbounded, 100,000 levels (a 100 KB file, well under
        MAX_YAML_BYTES) overflow the C stack and SEGFAULT ``CSafeLoader``,
        killing the supervisor on every tick, and make ``SafeLoader`` raise
        ``RecursionError``, which escapes ``except QueueSchemaError``."""
        path = tmp_path / "deep.yaml"
        path.write_bytes(b"id: x\ntitle: T\nprompt: P\ntags: " + b"[" * 100_000 + b"\n")
        assert path.stat().st_size < MAX_YAML_BYTES
        assert _load_task_in_child(path, yaml_backend) == (
            f"found a node nested more than {MAX_YAML_DEPTH} levels deep (MAX_YAML_DEPTH)\n"
        )


_LONE_SURROGATES = chr(0xD83D) + chr(0xDE00)
"""Two lone surrogates, built with chr(): CPython joins an escaped pair
written inside one string literal into a single character (U+1F600)."""

DIVERGENT_INPUTS: dict[str, tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = {
    "tab after a colon": ("title:\tT\n", {"title": "T"}, None),
    "trailing tab": ("title: T\t\n", {"title": "T"}, None),
    "tab inside a plain scalar": ("title: a\tb\n", {"title": "a\tb"}, None),
    "tab in a flow sequence": ("tags: [a,\tb]\n", {"tags": ["a", "b"]}, None),
    "escaped surrogate pair": ('title: "\\ud83d\\ude00"\n', None, {"title": _LONE_SURROGATES}),
    "%YAML 1.3 directive": ("%YAML 1.3\n---\ntitle: T\n", None, {"title": "T"}),
    "unknown directive": ("%FOO bar\n---\ntitle: T\n", None, {"title": "T"}),
}
"""case -> (YAML, what libyaml parses, what pure-python parses); ``None``
means that loader rejects the file as ``invalid YAML``. Pinned as parsed
mappings because that is where the loaders differ."""


class TestLoaderDivergence:
    """Inputs on which the two loaders disagree, pinned so that a PyYAML or
    LibYAML upgrade that changes one is noticed and ``_yaml_loader``'s
    docstring kept true. Only the scanners differ, and only on hand-written
    edge cases: ``write_task_atomic`` / ``write_state_atomic`` never emit
    any of them."""

    @pytest.mark.parametrize("case", sorted(DIVERGENT_INPUTS))
    def test_outcome(self, case: str, yaml_backend: str, queue_dir: Path) -> None:
        text, under_libyaml, under_pure_python = DIVERGENT_INPUTS[case]
        expected = under_libyaml if yaml_backend == "libyaml" else under_pure_python
        path = task_path_for(queue_dir, "x")
        path.write_text(text, encoding="utf-8")
        if expected is None:
            with pytest.raises(QueueSchemaError, match="invalid YAML"):
                _load_yaml(path)
        else:
            assert repr(_load_yaml(path)) == repr(expected)

    def test_pure_python_surrogates_are_not_the_character(
        self, monkeypatch: pytest.MonkeyPatch, queue_dir: Path
    ) -> None:
        """``SafeLoader`` accepting an escaped surrogate pair is no better
        than ``CSafeLoader`` rejecting it: the result is two lone surrogates,
        not the emoji, and text holding them cannot be encoded as UTF-8."""
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
        path = task_path_for(queue_dir, "x")
        path.write_text('title: "\\ud83d\\ude00"\n')
        title = _load_yaml(path)["title"]
        assert [hex(ord(ch)) for ch in title] == ["0xd83d", "0xde00"]
        with pytest.raises(UnicodeEncodeError):
            title.encode("utf-8")


def _missing(base: Path) -> Path:
    return base / "no-such-queue"


def _a_file(base: Path) -> Path:
    path = base / "queue.txt"
    path.write_text("", encoding="utf-8")
    return path


def _dangling_symlink(base: Path) -> Path:
    path = base / "link"
    path.symlink_to(base / "gone", target_is_directory=True)
    return path


NOT_A_QUEUE_DIR: dict[str, Callable[[Path], Path]] = {
    "missing": _missing,
    "a-file": _a_file,
    "dangling-symlink": _dangling_symlink,
}
"""Each way a ``--queue`` or a registered path can fail to be a directory."""


class TestRequireQueueDir:
    """The check that keeps ``queue_runtime_dir`` / ``todo_dir`` from creating a queue."""

    def test_existing_directory_is_returned_resolved(self, tmp_path: Path) -> None:
        queue = tmp_path / "q"
        queue.mkdir()
        assert require_queue_dir(queue) == queue.resolve()

    def test_relative_path_is_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "q").mkdir()
        monkeypatch.chdir(tmp_path)
        assert require_queue_dir(Path("q")) == (tmp_path / "q").resolve()

    def test_symlink_to_a_directory_is_followed(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        assert require_queue_dir(link) == real.resolve()

    @pytest.mark.parametrize("make", NOT_A_QUEUE_DIR.values(), ids=NOT_A_QUEUE_DIR.keys())
    def test_not_a_directory_raises_and_creates_nothing(
        self, tmp_path: Path, make: Callable[[Path], Path]
    ) -> None:
        base = tmp_path / "base"
        base.mkdir()
        path = make(base)
        before = sorted(p.name for p in base.iterdir())
        with pytest.raises(NotADirectoryError) as excinfo:
            require_queue_dir(path)
        assert str(excinfo.value) == f"not an existing directory: {path.resolve()}"
        assert sorted(p.name for p in base.iterdir()) == before

    @pytest.mark.skipif(os.geteuid() == 0, reason="root is not denied by directory permissions")
    def test_unsearchable_parent_counts_as_missing(self, tmp_path: Path) -> None:
        """Path.is_dir would raise PermissionError here on Python 3.12 and 3.13."""
        locked = tmp_path / "locked"
        queue = locked / "q"
        queue.mkdir(parents=True)
        locked.chmod(0o000)
        try:
            with pytest.raises(NotADirectoryError) as excinfo:
                require_queue_dir(queue)
        finally:
            locked.chmod(0o700)
        assert str(excinfo.value) == f"not an existing directory: {queue.resolve()}"
