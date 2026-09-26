r"""``--help`` must print the help text it is given, brackets and all.

Typer's default ``rich_markup_mode`` is ``"rich"`` whenever Rich is
installed (``typer.core.DEFAULT_MARKUP_MODE``). In that mode typer parses
every help string as Rich console markup, and Rich takes ``[`` followed by
a lowercase letter as the start of a style tag. So it silently dropped
config table names and type parameters from ``--help``. When this was
found on 2026-09-25, ten help texts in nine commands were affected.
``supervisor drain --help`` printed ``[supervisor].adopt_workers`` as
``.adopt_workers`` and ``[task_caps].max_duration_s_per_task`` as
``.max_duration_s_per_task``. ``queue restart-fresh --help`` printed
``[[accounts]]`` as ``[]``, and ``sidecar answer --help`` printed
``list[str]`` as ``list``. The rest were in ``doctor``, ``queue add``,
``queue backfill-working-dir``, ``supervisor stop``, ``usage refresh`` and
``usage whoami``.

Every ``typer.Typer`` in ``cli/`` now passes ``rich_markup_mode=None``, so
click prints help as written, in its plain format. This module checks
three things:

* Every command's ``--help``, rendered through the real entry point,
  contains every bracketed token of its source help text.
* Every ``typer.Typer`` in the command tree keeps ``rich_markup_mode=None``.
  Typer applies the root's mode to the whole tree, but a sub-app invoked
  on its own, as the unit tests do, uses its own.
* No help paragraph that depends on its line breaks is left for click to
  re-wrap. Click joins the lines of each paragraph and wraps them to the
  terminal, which turns an "Exit codes:" table or a bullet list into one
  run-on paragraph. A ``\b`` line before the paragraph keeps its lines.

Rich markup in ``console.print`` output is separate and still renders;
``TestConsoleMarkup`` pins that.
"""

from __future__ import annotations

import inspect
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any

import pytest
import typer
from typer.core import MarkupMode
from typer.testing import CliRunner

from claude_task_runner.cli import app

CLI: Any = typer.main.get_command(app)
"""The click command tree that ``claude-task-runner`` dispatches through.

Typed ``Any`` for the reason ``tests/unit/test_docs_cli_refs.py`` gives:
recent typer releases vendor click as ``typer._click``."""

_BRACKETED = re.compile(r"\[[^\[\]\n]+\]")
"""A bracketed token on one line: ``[queue]``, ``[str]``, or the inside of
``[[accounts]]``. Rich reads one that starts with a lowercase letter,
``#``, ``/`` or ``@`` as a markup tag. The gate checks every token, since
plain help must lose none of them."""

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
"""An ANSI escape sequence. Typer's Rich console forces colour when
``GITHUB_ACTIONS``, ``FORCE_COLOR`` or ``PY_COLORS`` is set."""

_LAID_OUT = re.compile(r"^(?:\s+\S|[*•-]\s|\d+[.)]\s)")
"""A line that is laid out rather than prose: indented, or a bullet or a
numbered item. Click would join it onto the line above."""

_NO_REWRAP = "\b"
"""Click's marker. A paragraph whose first line is exactly this keeps its
line breaks."""


def _path_id(path: tuple[str, ...]) -> str:
    return " ".join(path) or "<root>"


def _command_paths(node: Any = CLI, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every command path in the tree, including groups and the root ``()``."""
    paths = [prefix]
    for name, sub in sorted(getattr(node, "commands", {}).items()):
        paths.extend(_command_paths(sub, (*prefix, name)))
    return paths


def _node(path: tuple[str, ...], tree: Any = CLI) -> Any:
    node = tree
    for name in path:
        node = node.commands[name]
    return node


def _typer_apps(root: typer.Typer, name: str = "<root>") -> dict[str, typer.Typer]:
    """``root`` and every sub-app added under it, keyed by group name."""
    apps = {name: root}
    for group in root.registered_groups:
        assert group.typer_instance is not None, group.name
        apps.update(_typer_apps(group.typer_instance, str(group.name)))
    return apps


def _help_texts(node: Any) -> list[str]:
    """The help text that ``node``'s ``--help`` shows, as written in the source.

    That is the command's help up to any form feed (click drops the rest),
    its epilog, and the help of each parameter that is not hidden.
    """
    texts = [(node.help or "").partition("\f")[0], node.epilog or ""]
    texts.extend(
        getattr(param, "help", None) or ""
        for param in node.params
        if not getattr(param, "hidden", False)
    )
    return texts


def _tokens(texts: Iterable[str]) -> Counter[str]:
    return Counter(token for text in texts for token in _BRACKETED.findall(text))


def _squash(text: str) -> str:
    """``text`` without whitespace or ANSI escapes.

    Wrapping only inserts or replaces whitespace, at a hyphen too, so a
    token that survived rendering is still a substring after this.
    """
    return "".join(_ANSI.sub("", text).split())


def _render_help(root: typer.Typer, path: tuple[str, ...]) -> str:
    result = CliRunner().invoke(root, [*path, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def _lost_tokens(root: typer.Typer, path: tuple[str, ...]) -> dict[str, tuple[int, int]]:
    """Map each bracketed token that ``--help`` drops to ``(in source, in output)``.

    Empty when the rendered help shows every token at least as often as
    the source help text has it.
    """
    source = _tokens(_help_texts(_node(path, typer.main.get_command(root))))
    rendered = _squash(_render_help(root, path))
    counts = {token: (n, rendered.count(_squash(token))) for token, n in sorted(source.items())}
    return {token: (want, got) for token, (want, got) in counts.items() if got < want}


def _paragraphs(text: str) -> list[list[str]]:
    """Split help text on empty lines, as click's ``wrap_text`` does."""
    paragraphs: list[list[str]] = [[]]
    for line in inspect.cleandoc(text).splitlines():
        if line:
            paragraphs[-1].append(line)
        elif paragraphs[-1]:
            paragraphs.append([])
    return [lines for lines in paragraphs if lines]


def _rewrapped_layouts(text: str) -> list[str]:
    """The paragraphs of ``text`` whose line breaks click would discard.

    Click re-wraps every paragraph whose first line is not ``\\b``. That is
    harmless for prose and wrong for a paragraph with a laid-out line after
    its first (see ``_LAID_OUT``).
    """
    return [
        "\n".join(lines)
        for lines in _paragraphs(text)
        if lines[0].strip() != _NO_REWRAP and any(_LAID_OUT.match(line) for line in lines[1:])
    ]


def _demo_command(
    wait: Annotated[
        bool, typer.Option(help="Wait up to ``[task_caps].max_duration_s_per_task``.")
    ] = True,
) -> None:
    """Drain the demo queue.

    Reads ``[[accounts]]`` and takes a ``list[str]``. Rich keeps ``[A-Z]``
    and ``[--no-wait]``, which cannot open a tag.
    """


def _demo_app(mode: MarkupMode) -> typer.Typer:
    """A one-command app whose help has brackets Rich drops and brackets it keeps."""
    demo = typer.Typer(rich_markup_mode=mode)
    demo.command()(_demo_command)
    return demo


class TestChecker:
    """The checker itself. One that found nothing would pass every command."""

    def test_finds_every_bracketed_token(self) -> None:
        texts = ["``[queue]`` and ``[[accounts]]``", "list[str], [A-Z], [--no-wait], [queue]"]
        assert _tokens(texts) == Counter(
            {"[queue]": 2, "[accounts]": 1, "[str]": 1, "[A-Z]": 1, "[--no-wait]": 1}
        )

    def test_ignores_wrapping_and_ansi(self) -> None:
        assert _squash("up to ``[task_\n      caps]`` \x1b[1m[str]\x1b[0m") == (
            "upto``[task_caps]``[str]"
        )

    def test_rich_mode_loses_exactly_the_tag_shaped_tokens(self) -> None:
        # "rich" is typer.core.DEFAULT_MARKUP_MODE whenever Rich is
        # installed, so every command rendered this way before the fix.
        assert _lost_tokens(_demo_app("rich"), ()) == {
            "[accounts]": (1, 0),
            "[str]": (1, 0),
            "[task_caps]": (1, 0),
        }

    def test_plain_mode_keeps_every_token(self) -> None:
        assert _lost_tokens(_demo_app(None), ()) == {}

    def test_catches_the_shipped_defect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Put every app in the real tree back on typer's default mode, as the
        # CLI shipped before the fix.
        for sub_app in _typer_apps(app).values():
            monkeypatch.setattr(sub_app, "rich_markup_mode", "rich")
        assert _lost_tokens(app, ("supervisor", "drain")) == {
            "[supervisor]": (1, 0),
            "[task_caps]": (1, 0),
        }
        assert _lost_tokens(app, ("queue", "restart-fresh")) == {"[accounts]": (1, 0)}


class TestHelpKeepsBrackets:
    def test_every_typer_prints_help_as_written(self) -> None:
        apps = _typer_apps(app)
        # Guards the check below: the walk must reach every sub-app. Every
        # top-level command is a sub-app; none is registered on the root.
        assert set(apps) == {"<root>", *CLI.commands}
        markup = {
            name: sub_app.rich_markup_mode
            for name, sub_app in apps.items()
            if sub_app.rich_markup_mode is not None
        }
        assert markup == {}, (
            f"These typer.Typer apps parse help text as markup: {markup}. Markup "
            "drops bracketed words such as [queue]. Pass rich_markup_mode=None."
        )

    def test_scan_finds_the_known_tokens(self) -> None:
        # Guards the parametrised gate below against a pattern or a walk
        # that silently finds nothing.
        assert ("supervisor", "drain") in _command_paths()
        assert _tokens(_help_texts(_node(("supervisor", "drain")))) == Counter(
            {"[supervisor]": 1, "[task_caps]": 1}
        )

    @pytest.mark.parametrize("path", _command_paths(), ids=_path_id)
    def test_help_prints_every_bracketed_token(self, path: tuple[str, ...]) -> None:
        lost = _lost_tokens(app, path)
        assert not lost, (
            f"`claude-task-runner {_path_id(path)} --help` drops bracketed text:\n"
            + "\n".join(
                f"  {token}: {want} in the help text, {got} in --help"
                for token, (want, got) in lost.items()
            )
            + "\nTyper is parsing the help as markup. Every typer.Typer in "
            "src/claude_task_runner/cli/ must pass rich_markup_mode=None."
        )


class TestLayout:
    def test_flags_an_unmarked_table(self) -> None:
        text = "Summary.\n\nExit codes:\n  0  clean\n  1  parse drift\n"
        assert _rewrapped_layouts(text) == ["Exit codes:\n  0  clean\n  1  parse drift"]

    def test_flags_bullets_and_numbered_items(self) -> None:
        assert _rewrapped_layouts("* one\n  more\n* two") == ["* one\n  more\n* two"]
        assert _rewrapped_layouts("Steps:\n1. fetch\n2. merge") == ["Steps:\n1. fetch\n2. merge"]

    def test_passes_marked_blocks_and_prose(self) -> None:
        text = (
            "Summary.\n\n\b\nExit codes:\n  0  clean\n\n"
            "Prose that wraps\nonto a second line.\n\n"
            "    one indented example line"
        )
        assert _rewrapped_layouts(text) == []

    def test_marker_keeps_the_lines(self) -> None:
        # Click honours the marker: drain's help prints its table row by row.
        assert (
            "\n  Exit codes:\n"
            "    0  supervisor exited cleanly (or --no-wait and signal delivered)\n"
            "    1  no PID file / stale PID file\n"
            "    2  signal delivery rejected (permission)\n"
            "    4  --wait timed out (supervisor still draining — re-run drain or stop)\n"
        ) in _render_help(app, ("supervisor", "drain"))

    @pytest.mark.parametrize("path", _command_paths(), ids=_path_id)
    def test_no_laid_out_paragraph_is_rewrapped(self, path: tuple[str, ...]) -> None:
        bad = [para for text in _help_texts(_node(path)) for para in _rewrapped_layouts(text)]
        assert not bad, (
            f"`claude-task-runner {_path_id(path)} --help` would re-wrap these "
            "paragraphs into run-on text:\n\n"
            + "\n\n".join(bad)
            + "\n\nPut a line holding only \\b (click's no-rewrap marker) before "
            "each one."
        )


class TestConsoleMarkup:
    def test_console_print_still_renders_markup(self, tmp_path: Path) -> None:
        # rich_markup_mode governs help text only. `queue list` prints
        # "[dim]No pending tasks in todo/.[/]" through its own rich Console,
        # and the tags must still be consumed rather than printed.
        result = CliRunner().invoke(app, ["queue", "list", "--queue", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert _ANSI.sub("", result.output) == "No pending tasks in todo/.\n"
