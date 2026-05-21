"""Tests for ``runner.add_dirs`` — the ``--add-dir`` resolver.

The resolver decides what scope to widen the dispatched claude
sandbox to. Three rules:

* The queue directory is always first (it holds the sidecar protocol,
  reports/, and most source-file layouts).
* Per-task ``additional_dirs`` are merged in declared order, dedup'd
  against the queue dir.
* If ``auto_detect=True``, absolute paths embedded in the prompt
  that resolve to existing directories are appended (dedup'd against
  the above).

Missing per-task entries warn and are dropped; missing auto-detected
candidates are silently skipped (the regex finds many strings that
aren't directories).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.add_dirs import (
    _detect_paths_in_prompt,
    resolve_add_dirs,
)


def _task(
    *,
    prompt: str = "do the thing",
    additional_dirs: list[Path] | None = None,
) -> Task:
    return Task(
        id="t1",
        title="t",
        prompt=prompt,
        model="claude-opus-4-7",
        effort="medium",
        additional_dirs=list(additional_dirs or []),
    )


class TestResolveAddDirs:
    def test_default_is_queue_dir_only(self, tmp_path: Path) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        result = resolve_add_dirs(_task(), queue)
        assert result == [queue.resolve()]

    def test_explicit_additional_dirs_appended(self, tmp_path: Path) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        extra1 = tmp_path / "shared"
        extra1.mkdir()
        extra2 = tmp_path / "data"
        extra2.mkdir()

        result = resolve_add_dirs(
            _task(additional_dirs=[extra1, extra2]),
            queue,
        )
        assert result == [queue.resolve(), extra1.resolve(), extra2.resolve()]

    def test_dedup_against_queue_dir(self, tmp_path: Path) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        # Listing the queue dir again should be collapsed.
        result = resolve_add_dirs(
            _task(additional_dirs=[queue, queue]),
            queue,
        )
        assert result == [queue.resolve()]

    def test_dedup_within_additional_dirs(self, tmp_path: Path) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        extra = tmp_path / "shared"
        extra.mkdir()
        result = resolve_add_dirs(
            _task(additional_dirs=[extra, extra, extra]),
            queue,
        )
        assert result == [queue.resolve(), extra.resolve()]

    def test_missing_additional_dir_warns_and_skips(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        present = tmp_path / "real"
        present.mkdir()
        absent = tmp_path / "nope"  # never created

        with caplog.at_level(logging.WARNING, logger="claude_task_runner.runner.add_dirs"):
            result = resolve_add_dirs(
                _task(additional_dirs=[present, absent]),
                queue,
            )

        assert result == [queue.resolve(), present.resolve()]
        # Warning mentions the missing path so the operator can fix it.
        assert any("nope" in rec.message for rec in caplog.records)

    def test_file_instead_of_dir_warns_and_skips(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        a_file = tmp_path / "not_a_dir.txt"
        a_file.write_text("hello")

        with caplog.at_level(logging.WARNING, logger="claude_task_runner.runner.add_dirs"):
            result = resolve_add_dirs(
                _task(additional_dirs=[a_file]),
                queue,
            )

        assert result == [queue.resolve()]
        assert any("not_a_dir" in rec.message for rec in caplog.records)

    def test_missing_queue_dir_warns(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        missing_queue = tmp_path / "ghost_queue"  # never created
        with caplog.at_level(logging.WARNING, logger="claude_task_runner.runner.add_dirs"):
            result = resolve_add_dirs(_task(), missing_queue)
        # Without the queue dir we get an empty list (no dirs to widen
        # to); the warning surfaces the misconfiguration.
        assert result == []
        assert any("queue_dir" in rec.message for rec in caplog.records)

    def test_auto_detect_off_by_default(self, tmp_path: Path) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        existing = tmp_path / "auto_dir"
        existing.mkdir()
        result = resolve_add_dirs(
            _task(prompt=f"see {existing}/file.pdf"),
            queue,
        )
        # Off by default: not picked up even though the path exists.
        assert result == [queue.resolve()]

    def test_auto_detect_picks_only_existing_dirs(self, tmp_path: Path) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        existing = tmp_path / "auto_dir"
        existing.mkdir()
        # File doesn't actually need to exist for the regex to match;
        # the resolver walks up to the containing directory.
        file_in_existing = existing / "file.pdf"

        prompt = f"Source: {file_in_existing}\nOther: /tmp/this/does/not/exist/anywhere.txt\n"
        result = resolve_add_dirs(
            _task(prompt=prompt),
            queue,
            auto_detect=True,
        )
        # `existing` (the containing dir of the prompt's file
        # reference) is picked up; the non-existent path's dirs
        # bottom out without ever finding an ancestor we'd allow.
        assert queue.resolve() in result
        assert existing.resolve() in result

    def test_auto_detect_walks_to_containing_dir(self, tmp_path: Path) -> None:
        """A prompt that names a file should add the containing
        directory, not the file itself (which isn't a valid --add-dir).
        """
        queue = tmp_path / "queue"
        queue.mkdir()
        papers = tmp_path / "papers" / "PMID_007"
        papers.mkdir(parents=True)
        pdf = papers / "PMID_007.pdf"
        pdf.write_text("")  # the file exists but is_dir() == False

        prompt = f"Extract popPK from {pdf}."
        result = resolve_add_dirs(
            _task(prompt=prompt),
            queue,
            auto_detect=True,
        )
        # The file itself is NOT in the result (not a directory); its
        # containing directory IS.
        assert pdf.resolve() not in result
        assert papers.resolve() in result

    def test_auto_detect_skips_root_and_top_level(self, tmp_path: Path) -> None:
        """A bare ``/`` or ``/home`` mention in the prompt must not
        widen scope to the entire system — auto-detect is a
        convenience, not a footgun.
        """
        queue = tmp_path / "queue"
        queue.mkdir()
        # /home almost certainly exists on the test host; ensure the
        # resolver still refuses to add it (top-level dir blocklist).
        result = resolve_add_dirs(
            _task(prompt="something happens under /home or /var or /"),
            queue,
            auto_detect=True,
        )
        assert Path("/home") not in result
        assert Path("/var") not in result
        assert Path("/") not in result

    def test_auto_detect_dedups_against_explicit(self, tmp_path: Path) -> None:
        queue = tmp_path / "queue"
        queue.mkdir()
        shared = tmp_path / "shared"
        shared.mkdir()

        prompt = f"Use {shared} for staging."
        result = resolve_add_dirs(
            _task(prompt=prompt, additional_dirs=[shared]),
            queue,
            auto_detect=True,
        )
        # `shared` appears in both the additional_dirs list and the
        # prompt; result should contain it exactly once.
        assert result.count(shared.resolve()) == 1


class TestTruncatePathsForLog:
    """``_truncate_paths_for_log`` keeps the per-dispatch log line one-line
    even when the resolved add_dirs list is pathologically long. The
    operator's view is journalctl-friendly, so we trade detail for
    legibility past ~300 chars."""

    def test_empty_renders_as_brackets(self) -> None:
        from claude_task_runner.runner.dispatcher import _truncate_paths_for_log

        assert _truncate_paths_for_log([]) == "[]"

    def test_short_list_renders_in_full(self) -> None:
        from claude_task_runner.runner.dispatcher import _truncate_paths_for_log

        result = _truncate_paths_for_log([Path("/a"), Path("/b/c")])
        assert result == "[/a, /b/c]"

    def test_long_list_is_truncated_with_marker(self) -> None:
        from claude_task_runner.runner.dispatcher import _truncate_paths_for_log

        # Build a list whose rendered form is well over the 300-char cap.
        many = [Path(f"/very/long/queue/path/dir_{i:03d}") for i in range(40)]
        result = _truncate_paths_for_log(many)
        assert result.endswith("...]")
        # The cap is approximate; just check we're not far over.
        assert len(result) <= 305


class TestDetectPathsInPrompt:
    def test_extracts_absolute_paths(self) -> None:
        prompt = "Read /home/bill/papers/X.pdf and write to /tmp/out.md"
        paths = _detect_paths_in_prompt(prompt)
        assert Path("/home/bill/papers/X.pdf") in paths
        assert Path("/tmp/out.md") in paths

    def test_strips_trailing_punctuation(self) -> None:
        prompt = "See /home/bill/data/."
        paths = _detect_paths_in_prompt(prompt)
        # Trailing dot should be stripped before path parsing.
        assert Path("/home/bill/data") in paths

    def test_dedups(self) -> None:
        prompt = "/a/b /a/b /a/b"
        paths = _detect_paths_in_prompt(prompt)
        assert paths == [Path("/a/b")]

    def test_skips_intra_word_slashes(self) -> None:
        # A slash inside a word (e.g. "and/or") shouldn't match.
        prompt = "and/or sometimes happens"
        paths = _detect_paths_in_prompt(prompt)
        assert paths == []
