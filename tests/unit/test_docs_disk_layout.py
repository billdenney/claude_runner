"""Every file in architecture.md's on-disk layout is named by the code.

The per-queue tree in ``docs/architecture.md`` listed ``drift.log``
("parser drift + healthcheck results") and ``banner.txt`` ("human-readable
status banner") from the initial implementation on, but nothing ever wrote
either file. ``git log -S "drift.log" -- src/`` finds no commit that did,
and ``banner.txt`` was named only by the ``[notify]`` settings that the
2026-06-13 dead-config audit deleted. ``docs/runbook.md`` and
``docs/cheatsheet.md`` meanwhile sent operators to ``tail -F`` the drift
log. The architecture doc's header asks every PR that adds an on-disk file
to update the tree; nothing asked for a file that was never built to come
out of it. This test does.

It checks names, not placement: ``watchdog.log`` sat in the per-queue tree
while ``cron/watchdog.sh`` writes it under ``~/.claude_task_runner/``, and
a name check cannot see that. Nor does it check the reverse, that every
file the code writes has a tree entry.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
ARCHITECTURE = REPO_ROOT / "docs" / "architecture.md"
SRC_DIR = REPO_ROOT / "src" / "claude_task_runner"
SKILLS_DIR = SRC_DIR / "skills"

LAYOUT_HEADING = "## On-disk layout (per queue)"
"""The section holding both trees: the per-queue one, then the global one."""

_FENCE = re.compile(r"^```+\s*(\S*)\s*$")
"""A fence line; group 1 is the info string, empty for the trees."""

_TREE_PREFIX = re.compile(r"^[\s│├└─]*")
"""Indentation and box-drawing characters in front of a tree entry."""

_PLACEHOLDER = re.compile(r"<[^>]*>|NNN")
"""A templated part of a name (``<id>``, ``<ts>``, ``NNN``). The code builds
it at run time, so only the literal text around it can be looked up."""


def _layout_entries(text: str) -> list[tuple[int, str]]:
    """``(line_number, path)`` for every entry in the layout section's trees.

    Reads the untagged fenced blocks between :data:`LAYOUT_HEADING` and the
    next ``## `` heading; a table or a tagged block (```` ```toml ````) in
    the section is not a tree. A line holding only a ``#`` comment continues
    the entry above it and is skipped.

    Fails loud rather than returning less: a missing heading, a section with
    no tree entries, and an entry that is not a single path token all raise
    :class:`ValueError`.
    """
    lines = text.splitlines()
    try:
        start = lines.index(LAYOUT_HEADING)
    except ValueError:
        raise ValueError(f"no {LAYOUT_HEADING!r} section") from None

    entries: list[tuple[int, str]] = []
    in_fence = in_tree = False
    for lineno, line in enumerate(lines[start + 1 :], start + 2):
        if line.startswith("## "):
            break
        fence = _FENCE.match(line)
        if fence is not None:
            if in_fence:
                in_fence = in_tree = False
            else:
                in_fence, in_tree = True, fence.group(1) == ""
            continue
        if not in_tree:
            continue
        path = _TREE_PREFIX.sub("", line.split("#", 1)[0]).strip()
        if not path:
            continue
        if len(path.split()) != 1:
            raise ValueError(f"line {lineno}: tree entry is not one path: {line.strip()!r}")
        entries.append((lineno, path))

    if not entries:
        raise ValueError(f"no tree entries under {LAYOUT_HEADING!r}")
    return entries


def _literal_parts(path: str) -> list[str]:
    """The parts of ``path`` that the code has to spell out.

    Placeholders split a segment, so ``request-NNN.json`` gives ``request-``
    and ``.json``. The ``~`` home root is dropped, and so is any part with no
    letter or digit in it, such as the ``.`` between ``<id>`` and ``<ts>``.
    """
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "~"):
            continue
        parts.extend(
            part for part in _PLACEHOLDER.split(segment) if any(ch.isalnum() for ch in part)
        )
    return parts


def _py_strings(source: str) -> set[str]:
    """String literals in Python ``source``, including the literal parts of
    f-strings.

    A string that is a statement on its own (a module, class, function or
    attribute docstring) documents the code rather than being used by it, so
    it is left out.
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def _shell_strings(source: str) -> set[str]:
    """The lines of a shell script, minus blank lines and full-line comments
    (which include the shebang)."""
    return {
        line for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")
    }


def _toml_strings(source: str) -> set[str]:
    """Every string value in a TOML document. Keys and comments are left out."""
    found: set[str] = set()
    pending: list[object] = [tomllib.loads(source)]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


_READERS = {".py": _py_strings, ".sh": _shell_strings, ".toml": _toml_strings}


def _code_files() -> list[Path]:
    """The runner's code files, by the suffixes :data:`_READERS` can read.

    ``skills/`` is left out. Its SKILL.md files and helper scripts are
    agent-facing documentation and tooling that read the runner's files;
    none of them creates one.
    """
    return sorted(
        path
        for path in SRC_DIR.rglob("*")
        if path.suffix in _READERS and path.is_file() and SKILLS_DIR not in path.parents
    )


def _code_strings() -> set[str]:
    found: set[str] = set()
    for path in _code_files():
        found |= _READERS[path.suffix](path.read_text())
    return found


def _missing(entries: list[tuple[int, str]], strings: set[str]) -> list[tuple[int, str, list[str]]]:
    """Entries with a literal part that none of ``strings`` contains, each
    with the parts that were not found."""
    missing: list[tuple[int, str, list[str]]] = []
    for lineno, path in entries:
        absent = [part for part in _literal_parts(path) if not any(part in s for s in strings)]
        if absent:
            missing.append((lineno, path, absent))
    return missing


_SAMPLE_DOC = """\
# Architecture

## On-disk layout (per queue)

```
<queue>/
└── .claude_task_runner/            # all runtime state lives here
    ├── state/<id>.yaml             # TaskState: the
    │                               #   single source of truth per task
    ├── sidecar/<id>/request-NNN.json
    └── ema.json                    # per-task-type EMA values
```

Global (cross-queue):

```
~/.claude_task_runner/
└── global.lock                     # fcntl lock
```

| Started by | Supervisor log |
|---|---|
| cron watchdog | `supervisor.log` |

```toml
[logging]
level = "DEBUG"
```

## Key invariants

```
not-a-layout-entry.txt
```
"""


class TestLayoutParser:
    """The tree parser itself: one that finds nothing would pass everything."""

    def test_reads_both_trees_and_nothing_else(self) -> None:
        # Comment-only continuation lines, the table, the ```toml block and
        # the fence under the next section are all skipped.
        assert _layout_entries(_SAMPLE_DOC) == [
            (6, "<queue>/"),
            (7, ".claude_task_runner/"),
            (8, "state/<id>.yaml"),
            (10, "sidecar/<id>/request-NNN.json"),
            (11, "ema.json"),
            (17, "~/.claude_task_runner/"),
            (18, "global.lock"),
        ]

    def test_missing_section_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="On-disk layout"):
            _layout_entries(_SAMPLE_DOC.replace(LAYOUT_HEADING, "## Files"))

    def test_section_without_a_tree_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="no tree entries"):
            _layout_entries(f"{LAYOUT_HEADING}\n\nProse only.\n\n## Next\n")

    def test_unreadable_entry_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="ema json"):
            _layout_entries(_SAMPLE_DOC.replace("ema.json", "ema json"))


class TestLiteralParts:
    @pytest.mark.parametrize(
        ("path", "parts"),
        [
            ("ema.json", ["ema.json"]),
            ("request-NNN.json", ["request-", ".json"]),
            ("attempt-<N>.stream.jsonl", ["attempt-", ".stream.jsonl"]),
            ("state/.corrupt/<id>.<ts>.yaml", ["state", ".corrupt", ".yaml"]),
            ("~/.claude_task_runner/", [".claude_task_runner"]),
            ("<queue>/", []),
        ],
    )
    def test_splits_on_placeholders(self, path: str, parts: list[str]) -> None:
        assert _literal_parts(path) == parts


class TestCodeStrings:
    """What counts as the code naming a file. Prose must not."""

    def test_python_literals_count_and_docstrings_do_not(self) -> None:
        source = (
            '"""Module docstring naming ghost.log."""\n'
            'NAME = "ema.json"\n'
            '"""Attribute docstring naming ghost.log."""\n'
            "def path(n: int) -> str:\n"
            '    """Function docstring naming ghost.log."""\n'
            '    return f"attempt-{n}.stream.jsonl"\n'
        )
        strings = _py_strings(source)
        assert {"ema.json", "attempt-", ".stream.jsonl"} <= strings
        assert not any("ghost.log" in s for s in strings)

    def test_shell_code_counts_and_comments_do_not(self) -> None:
        source = '#!/usr/bin/env bash\n# Logs to ~/ghost.log\nLOG_FILE="${LOG_DIR}/watchdog.log"\n'
        assert _shell_strings(source) == {'LOG_FILE="${LOG_DIR}/watchdog.log"'}

    def test_toml_values_count_and_keys_and_comments_do_not(self) -> None:
        source = (
            "[supervisor]\n"
            'state_file = "supervisor.json"  # not ghost.log\n'
            'captures = ["a.cap"]\n'
            "[ghost.table]\n"
            "count = 1\n"
        )
        assert _toml_strings(source) == {"supervisor.json", "a.cap"}

    def test_code_files_cover_each_reader_and_skip_skills(self) -> None:
        files = _code_files()
        assert SRC_DIR / "cron" / "watchdog.sh" in files
        assert SRC_DIR / "config" / "defaults" / "settings.toml" in files
        assert SRC_DIR / "supervisor" / "pidfile.py" in files
        assert not [path for path in files if SKILLS_DIR in path.parents]


class TestMissing:
    def test_reports_the_entries_no_code_names(self) -> None:
        # drift.log and banner.txt: the two entries this gate was written for.
        entries = [
            (1, "drift.log"),
            (2, "banner.txt"),
            (3, "supervisor.pid"),
            (4, "logs/<id>/attempt-<N>.stderr"),
        ]
        strings = {"supervisor.pid", "logs", "attempt-", ".stderr"}
        assert _missing(entries, strings) == [
            (1, "drift.log", ["drift.log"]),
            (2, "banner.txt", ["banner.txt"]),
        ]

    def test_every_literal_part_must_be_found(self) -> None:
        assert _missing([(1, "request-NNN.json")], {"request-"}) == [
            (1, "request-NNN.json", [".json"])
        ]


class TestLayoutMatchesCode:
    def test_both_trees_are_read(self) -> None:
        # Guards the check below: a parser that read one tree, or stopped
        # partway through one, would pass on less than the whole layout. The
        # first and last entry of each tree are the known answers.
        paths = {path for _, path in _layout_entries(ARCHITECTURE.read_text())}
        assert {"claude_runner.toml", "ema.json", "global.lock", "crontab.backup.<ts>"} <= paths

    def test_every_layout_entry_is_named_in_code(self) -> None:
        missing = _missing(_layout_entries(ARCHITECTURE.read_text()), _code_strings())
        assert not missing, (
            "docs/architecture.md's on-disk layout names file(s) that no code names,\n"
            "so an operator sent to read one finds nothing there. Each entry's\n"
            "literal text must appear in a string literal in a .py file under\n"
            "src/claude_task_runner/ (docstrings do not count) or in a .sh or .toml\n"
            "file there (comments do not count); skills/ is not searched.\n"
            + "\n".join(
                f"  docs/architecture.md:{lineno}: {path} (not found: {', '.join(parts)})"
                for lineno, path, parts in missing
            )
            + "\nRemove the entry, or correct it to the name the code writes."
        )
