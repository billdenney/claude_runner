"""Every TOML example in settings.toml's comments loads.

``settings.toml`` is the one place an operator is shown what an account's
``runner-account.toml`` holds. After ADR-0022 replaced ``[throttle.*]`` with
``[dispatch_pct.*]`` and made the loader reject any ``throttle`` key, that
example still showed ``[throttle.five_hour]`` and ``[throttle.time_of_day]``:
an operator who copied it got a file the loader refuses.
``tests/unit/test_docs_config_refs.py`` checks the config references in
``docs/``, not the comments here. This test extracts each commented example
and loads it the way the runner loads the file it belongs in.
"""

from __future__ import annotations

import re
import textwrap
import tomllib
from importlib import resources
from pathlib import Path

import pytest
from pydantic import BaseModel

from claude_task_runner.config.loader import (
    PER_ACCOUNT_TOML_NAME,
    ConfigError,
    load_account_policy,
    load_settings,
)
from claude_task_runner.config.schema import (
    AccountDispatchPolicy,
    AccountPolicy,
    DispatchPctSettings,
    Settings,
)
from claude_task_runner.throttle.policy import resolve

SETTINGS_TEXT = (
    resources.files("claude_task_runner.config.defaults")
    .joinpath("settings.toml")
    .read_text(encoding="utf-8")
)
"""The file :func:`config.loader.load_defaults` reads."""

QUEUE_EXAMPLE = "Example two-account setup"
"""Introduces the ``[[accounts]]`` example, part of a ``claude_runner.toml``."""

ACCOUNT_EXAMPLE = "Each account's own runner-account.toml then carries:"
"""Introduces the example of an account's ``runner-account.toml``."""

CHECKED_EXAMPLES = (QUEUE_EXAMPLE, ACCOUNT_EXAMPLE)
"""Every commented example the tests below load. A new one in settings.toml
fails :meth:`TestSettingsExamples.test_every_toml_example_is_checked` until
it is added here, with a test that loads it."""

_PROSE = re.compile(r"^# \S")
"""A line of a comment paragraph: ``#``, one space, text."""

_INDENTED = re.compile(r"^#\s{2,}\S")
"""A line of an indented block, such as an example: ``#`` and two or more spaces."""

_BLANK = re.compile(r"^#\s*$")
"""An empty comment line, between paragraphs or between an example's tables."""

_TABLE_HEADER = re.compile(r"^\[\[?[a-z_][\w.]*\]\]?$")
"""``[table]`` or ``[[array]]``: how a TOML example starts."""


def _comment_blocks(text: str) -> list[tuple[int, str]]:
    """``(line_number, block)`` for every indented block in ``text``'s comments.

    A block is a run of indented comment lines, with the empty comment lines
    inside it, ended by a paragraph line or a line that is not a comment.
    The ``#`` and the common indent are stripped, so an example comes out as
    the TOML an operator would copy. ``line_number`` is its first line.
    """
    lines = text.splitlines()
    blocks: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        if not _INDENTED.match(lines[i]):
            i += 1
            continue
        start = i
        while i < len(lines) and (_INDENTED.match(lines[i]) or _BLANK.match(lines[i])):
            i += 1
        body = textwrap.dedent("\n".join(line[1:] for line in lines[start:i]))
        blocks.append((start + 1, body.strip("\n") + "\n"))
    return blocks


def _is_toml_example(block: str) -> bool:
    """Whether ``block`` is a TOML example: its first line that is not a
    TOML comment is a table header. Bullets, paths and a lone
    ``key = value`` inside prose are not."""
    for line in block.splitlines():
        code = line.split("#", 1)[0].strip()
        if code:
            return _TABLE_HEADER.match(code) is not None
    return False


def _example_after(text: str, lead_in: str) -> tuple[int, str]:
    """The block right after the comment paragraph containing ``lead_in``.

    Only empty comment lines may separate the paragraph from the block.
    Fails loud rather than returning less: ``lead_in`` on no line or on
    several, or a paragraph with no block right after it, raises
    :class:`ValueError`. A deleted example must fail the gate, not load as
    an empty file, which is all defaults, or as the next section's block.
    """
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if lead_in in line]
    if len(hits) != 1:
        raise ValueError(f"{lead_in!r} is on {len(hits)} lines, not one")
    i = hits[0] + 1
    while i < len(lines) and _PROSE.match(lines[i]):
        i += 1
    while i < len(lines) and _BLANK.match(lines[i]):
        i += 1
    for lineno, block in _comment_blocks(text):
        if lineno == i + 1:
            return lineno, block
    raise ValueError(f"no indented block right after the paragraph containing {lead_in!r}")


def _load_account_policy(example: str, tmp_path: Path) -> AccountPolicy:
    """Load ``example`` as an account's ``runner-account.toml``."""
    (tmp_path / PER_ACCOUNT_TOML_NAME).write_text(example, encoding="utf-8")
    return load_account_policy(str(tmp_path))


def _load_queue_settings(example: str, tmp_path: Path) -> Settings:
    """Load ``example`` as a queue's ``claude_runner.toml``."""
    toml = tmp_path / "claude_runner.toml"
    toml.write_text(example, encoding="utf-8")
    return load_settings(toml)


def _key_tree(model: type[BaseModel]) -> dict[str, object]:
    """``model``'s TOML keys, each sub-model's keys nested under its name.

    A key that holds a value rather than a table maps to ``None``.
    """
    tree: dict[str, object] = {}
    for name, field in model.model_fields.items():
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            tree[name] = _key_tree(annotation)
        else:
            tree[name] = None
    return tree


_SAMPLE = """\
[claude]
executable = "claude"

# Example setup (two accounts,
# one per person):
#
#   [[accounts]]
#   name = "a"
#
#   [[accounts]]
#   name = "b"
#
# Each account's file then carries:
#
#   [concurrency]
#   max_concurrency = 2
#
# Bullets are indented too, and are not TOML:
#
#   * "tty" - read the TUI
# ---------------------------------------------------------------------------
[usage]
source = "tty"
"""

_RETIRED_EXAMPLE = """\
# Each account's own runner-account.toml then carries:
#
#   [concurrency]
#   max_concurrency = 5
#
#   [throttle.five_hour]
#   daytime_band_full_dispatch_max_pct   = 40
#   daytime_band_slowdown_max_pct        = 60
#   nighttime_band_full_dispatch_max_pct = 70
#   nighttime_band_slowdown_max_pct      = 90
#
#   [throttle.time_of_day]
#   day_end = "21:00"
#
# Account names appear in supervisor logs.
"""
"""The per-account example as settings.toml showed it before this gate, still
in the tables ADR-0022 retired."""


class _Leaf(BaseModel):
    depth: int


class _Tree(BaseModel):
    name: str | None = None
    leaf: _Leaf


class TestExtraction:
    """The extractor itself: one that finds nothing would pass everything."""

    def test_finds_every_indented_block(self) -> None:
        assert _comment_blocks(_SAMPLE) == [
            (7, '[[accounts]]\nname = "a"\n\n[[accounts]]\nname = "b"\n'),
            (15, "[concurrency]\nmax_concurrency = 2\n"),
            (20, '* "tty" - read the TUI\n'),
        ]

    def test_takes_the_block_after_a_multi_line_paragraph(self) -> None:
        assert _example_after(_SAMPLE, "Example setup") == (
            7,
            '[[accounts]]\nname = "a"\n\n[[accounts]]\nname = "b"\n',
        )

    def test_takes_the_block_after_a_one_line_paragraph(self) -> None:
        assert _example_after(_SAMPLE, "then carries:") == (
            15,
            "[concurrency]\nmax_concurrency = 2\n",
        )

    def test_missing_lead_in_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="on 0 lines"):
            _example_after(_SAMPLE, "no such paragraph")

    def test_repeated_lead_in_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="on 2 lines"):
            _example_after(_SAMPLE + "# Example setup, again.\n", "Example setup")

    def test_deleted_example_fails_loud(self) -> None:
        # The next paragraph follows the lead-in, and a later block exists.
        # Returning that block would check the wrong text.
        deleted = _SAMPLE.replace("#   [concurrency]\n#   max_concurrency = 2\n#\n", "")
        with pytest.raises(ValueError, match="no indented block"):
            _example_after(deleted, "then carries:")

    @pytest.mark.parametrize(
        ("block", "is_example"),
        [
            ("[concurrency]\nmax_concurrency = 2\n", True),
            ('[[accounts]]\nname = "a"\n', True),
            ("[dispatch_pct.day]  # a trailing comment\nfivehr_stop_pct = 60\n", True),
            # A note above the first table must not hide the example.
            ("# Optional.\n\n[concurrency]\nmax_concurrency = 2\n", True),
            ('* "tty" - read the TUI\n', False),
            ("<config_dir>/runner-account.toml\n", False),
            ('working_dir_template = "/repo/{task_id}"\n', False),
            ("# Only a comment.\n", False),
        ],
    )
    def test_a_toml_example_starts_with_a_table(self, block: str, is_example: bool) -> None:
        assert _is_toml_example(block) is is_example

    def test_key_tree_nests_sub_models(self) -> None:
        assert _key_tree(_Tree) == {"name": None, "leaf": {"depth": None}}


class TestSettingsExamples:
    def test_every_toml_example_is_checked(self) -> None:
        examples = {
            lineno for lineno, block in _comment_blocks(SETTINGS_TEXT) if _is_toml_example(block)
        }
        checked = {_example_after(SETTINGS_TEXT, lead_in)[0] for lead_in in CHECKED_EXAMPLES}
        assert examples == checked, (
            f"settings.toml has commented TOML examples at lines {sorted(examples)}; "
            f"CHECKED_EXAMPLES loads the ones at {sorted(checked)}. Add each new "
            "example's lead-in to CHECKED_EXAMPLES, with a test that loads it."
        )

    def test_the_accounts_example_loads_as_written(self, tmp_path: Path) -> None:
        _lineno, example = _example_after(SETTINGS_TEXT, QUEUE_EXAMPLE)
        settings = _load_queue_settings(example, tmp_path)
        payload = tomllib.loads(example)
        # An example that came out empty would have no "accounts" key.
        assert list(payload) == ["accounts"]
        # Loaded as declared, not replaced by the synthesised "default".
        loaded = [acct.model_dump(exclude_unset=True) for acct in settings.accounts]
        assert loaded == payload["accounts"]

    def test_the_account_example_loads_as_written(self, tmp_path: Path) -> None:
        _lineno, example = _example_after(SETTINGS_TEXT, ACCOUNT_EXAMPLE)
        policy = _load_account_policy(example, tmp_path)
        payload = tomllib.loads(example)
        # It shows every table the file takes. An example that came out
        # empty would load as all defaults and fail here.
        assert set(payload) == set(AccountPolicy.model_fields)
        # Every key it sets is loaded, with the value it shows.
        assert policy.model_dump(exclude_unset=True) == payload

    def test_the_account_example_keeps_the_bands_ordered(self) -> None:
        # throttle.policy.resolve fills each key the account leaves out from
        # the queue's [dispatch_pct.*] and does not re-check the order the
        # queue-wide schema enforces, so check the composed policy here.
        _lineno, example = _example_after(SETTINGS_TEXT, ACCOUNT_EXAMPLE)
        policy = AccountPolicy.model_validate(tomllib.loads(example))
        resolved = resolve(load_settings(), policy, account_name="example")
        assert resolved.day.fivehr_slowdown_pct < resolved.day.fivehr_stop_pct
        assert resolved.night.fivehr_slowdown_pct < resolved.night.fivehr_stop_pct
        assert resolved.week.early_pct < resolved.week.eow_pct

    def test_account_dispatch_pct_takes_the_queue_keys(self) -> None:
        # The example's comment says an account's [dispatch_pct.*] takes the
        # queue's keys (ADR-0022). A queue key with no per-account field
        # would make that false, and could not be overridden per account.
        assert _key_tree(AccountDispatchPolicy) == _key_tree(DispatchPctSettings)


class TestTheGateCatchesStaleExamples:
    """Known-bad examples: each must fail the way the gate loads it."""

    def test_the_retired_throttle_example(self, tmp_path: Path) -> None:
        _lineno, example = _example_after(_RETIRED_EXAMPLE, ACCOUNT_EXAMPLE)
        with pytest.raises(ConfigError, match=r"\[throttle\.\*\]"):
            _load_account_policy(example, tmp_path)

    def test_a_renamed_key(self, tmp_path: Path) -> None:
        _lineno, example = _example_after(SETTINGS_TEXT, ACCOUNT_EXAMPLE)
        assert "fivehr_stop_pct" in example
        renamed = example.replace("fivehr_stop_pct", "fivehr_stop_percent")
        with pytest.raises(ConfigError, match="fivehr_stop_percent"):
            _load_account_policy(renamed, tmp_path)
