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
five things:

* Every command's ``--help``, rendered through the real entry point,
  contains every bracketed token of its source help text.
* Every ``typer.Typer`` in the command tree keeps ``rich_markup_mode=None``.
  Typer applies the root's mode to the whole tree, but a sub-app invoked
  on its own, as the unit tests do, uses its own.
* No help paragraph that depends on its line breaks is left for click to
  re-wrap. Click joins the lines of each paragraph and wraps them to the
  terminal, which turns an "Exit codes:" table or a bullet list into one
  run-on paragraph. A ``\b`` line before the paragraph keeps its lines.
* No ``--help`` shows a Python repr as an option's default. Typer shows a
  callable default with ``str()`` unless it is a plain function, and every
  ``--queue`` defaults to the bound method ``Path.cwd``. When this was
  found on 2026-09-26, 22 of the 44 pages printed
  ``[default: <bound method Path.cwd of <class 'pathlib.Path'>>]``. Each
  ``--queue`` now passes ``show_default=CWD_DEFAULT_LABEL`` and prints
  ``[default: (current directory)]``. The label changes only the help, so
  ``TestQueueDefault`` checks that every ``--queue`` still defaults to the
  directory the command runs in.
* Every group whose app registers a callback with a docstring prints that
  docstring as its help. Typer prints ``add_typer``'s ``help=`` in its
  place. When this was found on 2026-09-26, ``cli/__init__.py`` passed a
  one-line ``help=`` to all four such groups. So ``install --help`` hid
  what its systemd and cron installs do, and ``usage --help`` hid that
  ``usage`` with no subcommand runs ``render``. They now pass
  ``short_help=``, the line the root listing prints, and
  ``TestGroupHelp`` pins those lines.

Rich markup in ``console.print`` output is separate and still renders;
``TestConsoleMarkup`` pins that.
"""

from __future__ import annotations

import inspect
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Annotated, Any
from unittest import mock

import pytest
import typer
from typer.core import MarkupMode
from typer.testing import CliRunner

from claude_task_runner.cli import app
from claude_task_runner.cli._helpers import CWD_DEFAULT_LABEL

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

_DEFAULT = re.compile(r"[\[;] ?default: ([^;\]]*)")
"""The value of the ``default:`` item in the brackets click prints after an
option's help: ``[default: 2.0]``, or ``[env var: X; default: 2.0; required]``.
Click wraps a long default onto the next line, so this is matched against
help whose whitespace is collapsed."""

_REPR_MARKERS = ("<bound method", "<function", "<class", "<built-in")
"""How the repr of a method, function, class or builtin starts. Typer shows
a callable default with ``str()``, which for these is the repr, unless it
is a plain function, which it shows as ``(dynamic)``."""


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
    its epilog, the help of each parameter that is not hidden, and the
    ``short_help`` of each subcommand that it lists. Click lists a
    ``short_help`` whole; a subcommand without one is listed with the start
    of its help, which may be cut short, so that is left out.
    """
    texts = [(node.help or "").partition("\f")[0], node.epilog or ""]
    texts.extend(
        getattr(param, "help", None) or ""
        for param in node.params
        if not getattr(param, "hidden", False)
    )
    texts.extend(
        sub.short_help or "" for sub in getattr(node, "commands", {}).values() if not sub.hidden
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


def _help_width(columns: int) -> int:
    """The width click lays help out at in a terminal ``columns`` wide.

    Asked of click outside its test runner, which lays help out 80
    columns wide whatever the terminal. A real 80-column terminal gets 78.
    """
    with mock.patch.dict(os.environ, {"COLUMNS": str(columns)}):
        ctx = CLI.make_context(CLI.name, [], resilient_parsing=True)
        return int(ctx.make_formatter().width)


def _render_help(root: typer.Typer, path: tuple[str, ...], columns: int | None = None) -> str:
    """``path``'s ``--help``, laid out as in a terminal ``columns`` wide if given."""
    extra = {} if columns is None else {"terminal_width": _help_width(columns)}
    result = CliRunner().invoke(root, [*path, "--help"], **extra)
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


def _defaults(output: str) -> list[str]:
    """Each option default that rendered ``--help`` shows, whitespace collapsed."""
    return _DEFAULT.findall(" ".join(_ANSI.sub("", output).split()))


def _repr_defaults(output: str) -> list[str]:
    """The defaults that rendered ``--help`` shows as a Python repr.

    Values are compared squashed, because click can break a line inside a
    repr, including at the hyphen in ``<built-in``.
    """
    markers = [_squash(marker) for marker in _REPR_MARKERS]
    return [value for value in _defaults(output) if any(m in _squash(value) for m in markers)]


def _queue_option(command: Any) -> Any:
    """``command``'s ``--queue`` option, or ``None`` when it has none."""
    return next((param for param in command.params if "--queue" in param.opts), None)


def _queue_paths() -> list[tuple[str, ...]]:
    """Every command path whose command takes ``--queue``."""
    return [path for path in _command_paths() if _queue_option(_node(path)) is not None]


def _parsed_queue(command: Any) -> Any:
    """The ``--queue`` that ``command`` gets when it is run without one.

    This parses the command line and does not run the command, since some
    commands start a supervisor or edit the crontab. ``resilient_parsing``
    lets a command with a required argument parse without it.
    """
    ctx = command.make_context(command.name, [], resilient_parsing=True)
    return ctx.params[_queue_option(command).name]


def _group_docstrings(
    root: typer.Typer = app, prefix: tuple[str, ...] = ()
) -> dict[tuple[str, ...], str]:
    """The callback docstring of each group in ``root``'s tree that has one.

    Keyed by command path. Typer gives the group the docstring of its
    app's callback as its help text, unless ``add_typer`` passes ``help=``.
    """
    docstrings: dict[tuple[str, ...], str] = {}
    for group in root.registered_groups:
        sub_app = group.typer_instance
        assert sub_app is not None, group.name
        path = (*prefix, str(group.name))
        registered = sub_app.registered_callback
        callback = registered.callback if registered is not None else None
        docstring = inspect.getdoc(callback) if callback is not None else None
        if docstring:
            docstrings[path] = docstring
        docstrings.update(_group_docstrings(sub_app, path))
    return docstrings


def _one_line_paragraphs(text: str) -> list[str]:
    """The paragraphs of ``text``, each joined onto one line.

    Click re-wraps each paragraph to the terminal, so this is what help
    text and its rendering have in common at any width. A ``\\b`` marker
    line is dropped, as click drops it.
    """
    paragraphs = []
    for lines in _paragraphs(text):
        kept = [line for line in lines if line.strip() != _NO_REWRAP]
        paragraphs.append(" ".join(" ".join(kept).split()))
    return paragraphs


def _page_paragraphs(path: tuple[str, ...], root: typer.Typer = app) -> list[str]:
    """The help text that ``path``'s rendered ``--help`` prints above its
    options, as one-line paragraphs."""
    output = _ANSI.sub("", _render_help(root, path))
    above_options, found, _ = output.partition("\nOptions:\n")
    assert found, output
    _usage, _, text = above_options.partition("\n\n")
    return _one_line_paragraphs(text)


_LISTING_COLUMNS = 80
"""The terminal width the listing checks render at. Click lays help out no
wider in a wider terminal, so a row that fits here fits there too."""


def _listing(path: tuple[str, ...], root: typer.Typer = app) -> dict[str, str]:
    """The ``Commands:`` section of ``path``'s ``--help`` in an 80-column
    terminal: each command's name, and its row's text on one line."""
    output = _ANSI.sub("", _render_help(root, path, columns=_LISTING_COLUMNS))
    _, found, section = output.partition("\nCommands:\n")
    assert found, output
    rows: dict[str, str] = {}
    name = ""
    for line in section.splitlines():
        if line.startswith("  ") and not line.startswith("   "):
            name, _, text = line.strip().partition(" ")
            rows[name] = text.strip()
        elif line.strip():
            rows[name] = f"{rows[name]} {line.strip()}".strip()
    return rows


def _cut_short(root: typer.Typer = app) -> dict[tuple[str, ...], dict[str, str]]:
    """The listing rows that end in ``...``, by group path and command name.

    Click lists a command by the first sentence of its help. When that
    sentence does not fit, click cuts it at a word and adds ``...``. A
    ``short_help`` is listed whole instead, wrapped if need be.
    """
    tree = typer.main.get_command(root)
    cut: dict[tuple[str, ...], dict[str, str]] = {}
    for path in _command_paths(tree):
        if getattr(_node(path, tree), "commands", None):
            rows = {
                name: text for name, text in _listing(path, root).items() if text.endswith("...")
            }
            if rows:
                cut[path] = rows
    return cut


def _demo_command(
    wait: Annotated[
        bool, typer.Option(help="Wait up to ``[task_caps].max_duration_s_per_task``.")
    ] = True,
) -> None:
    """Drain the demo queue.

    Reads ``[[accounts]]`` and takes a ``list[str]``. Rich keeps ``[A-Z]``
    and ``[--no-wait]``, which cannot open a tag.
    """


_OLD_QUEUE_OPTION = typer.Option(Path.cwd, "--queue", help="Queue directory.")
"""``--queue`` as every command declared it before the fix."""

_QUEUE_OPTION = typer.Option(
    Path.cwd, "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
)
"""``--queue`` as every command declares it now."""

_IMPORT_TIME_QUEUE_OPTION = typer.Option(
    Path.cwd(), "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
)
"""A ``--queue`` whose default is the directory this module was imported in.
Its help is the same as ``_QUEUE_OPTION``'s."""


def _old_queue_command(queue_dir: Path = _OLD_QUEUE_OPTION) -> None:
    """Read the demo queue."""


def _queue_command(queue_dir: Path = _QUEUE_OPTION) -> None:
    """Read the demo queue."""


def _import_time_queue_command(queue_dir: Path = _IMPORT_TIME_QUEUE_OPTION) -> None:
    """Read the demo queue."""


def _demo_app(mode: MarkupMode, command: Callable[..., None] = _demo_command) -> typer.Typer:
    """A one-command app. The default command's help has brackets Rich drops
    and brackets it keeps."""
    demo = typer.Typer(rich_markup_mode=mode)
    demo.command()(command)
    return demo


def _demo_group_callback() -> None:
    """Summarise the demo group.

    Its second paragraph runs over two lines, which click joins and
    wraps again to fit the terminal.

    \b
    Exit codes:
      0  clean
      1  drift
    """


def _demo_group(**add_typer: Any) -> typer.Typer:
    """A root app with one group, ``demo``, whose callback is
    ``_demo_group_callback``. ``add_typer`` goes to ``add_typer``."""
    group = typer.Typer(rich_markup_mode=None)
    group.callback()(_demo_group_callback)
    root = typer.Typer(rich_markup_mode=None)
    root.add_typer(group, name="demo", **add_typer)
    return root


def _demo_long_command() -> None:
    """Summarise what this demo command does, in more words than its row
    in the listing has room for.
    """


def _demo_short_command() -> None:
    """Summarise it briefly.

    A second sentence, which the listing leaves out.
    """


_DEMO_SHORT_HELP = (
    "A short help longer than the row has room for, which click wraps onto a second line."
)


def _demo_listing() -> typer.Typer:
    """A root app with one group, ``demo``, that lists a first sentence too
    long for its row, one that fits, and a ``short_help`` too long for it."""
    group = typer.Typer(rich_markup_mode=None)
    group.command("long")(_demo_long_command)
    group.command("short")(_demo_short_command)
    group.command("whole", short_help=_DEMO_SHORT_HELP)(_demo_short_command)
    root = typer.Typer(rich_markup_mode=None)
    root.add_typer(group, name="demo", help="Demo group.")
    return root


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


class TestDefaults:
    def test_finds_the_defaults_and_flags_the_reprs(self) -> None:
        # As click prints them: wrapped, once at the hyphen in "<built-in".
        text = (
            "Options:\n"
            "  --queue <path>  Queue directory.  [default: <bound method\n"
            "                  Path.cwd of <class 'pathlib.Path'>>]\n"
            "  --clock <name>  Clock.  [env var: CLOCK; default: <built-\n"
            "                  in function time>; required]\n"
            "  --kind <name>   Type.  [default: <class 'int'>]\n"
            "  --hook <name>   Hook.  [default: functools.partial(<function\n"
            "                  done at 0x7f>)]\n"
            "  --dir <path>    Work directory.  [default: (current directory)]\n"
            "  --at <time>     Sets the default: now.  [default: (dynamic)]\n"
            "  --wait <s>      Seconds.  [default: 2.0; 0<=x<=60]\n"
        )
        reprs = [
            "<bound method Path.cwd of <class 'pathlib.Path'>>",
            "<built- in function time>",
            "<class 'int'>",
            "functools.partial(<function done at 0x7f>)",
        ]
        assert _defaults(text) == [*reprs, "(current directory)", "(dynamic)", "2.0"]
        assert _repr_defaults(text) == reprs

    def test_flags_the_old_queue_option(self) -> None:
        # The repr names differ between Python versions: PathBase.cwd and
        # pathlib._local.Path on 3.13.
        help_text = _render_help(_demo_app(None, _old_queue_command), ())
        assert _repr_defaults(help_text) == [repr(Path.cwd)]

    def test_passes_the_new_queue_option(self) -> None:
        help_text = _render_help(_demo_app(None, _queue_command), ())
        assert _defaults(help_text) == ["(current directory)"]
        assert _repr_defaults(help_text) == []

    def test_scan_finds_the_known_default(self) -> None:
        # Guards the parametrised gate below against a pattern that
        # silently finds nothing.
        assert _defaults(_render_help(app, ("supervisor", "status"))) == ["(current directory)"]

    @pytest.mark.parametrize("path", _command_paths(), ids=_path_id)
    def test_no_default_is_a_python_repr(self, path: tuple[str, ...]) -> None:
        reprs = _repr_defaults(_render_help(app, path))
        assert not reprs, (
            f"`claude-task-runner {_path_id(path)} --help` shows a Python repr as a default:\n"
            + "\n".join(f"  [default: {value}]" for value in reprs)
            + "\nTyper shows a callable default with str() unless it is a plain "
            "function. Give the option a show_default string that describes the "
            "default; for Path.cwd, show_default=CWD_DEFAULT_LABEL from "
            "cli/_helpers.py."
        )


class TestQueueDefault:
    """``--queue`` defaults to the directory the command runs in, as help says.

    ``show_default`` changes only what help shows. A ``--queue`` whose
    default was fixed at import, ``Path.cwd()`` in place of ``Path.cwd``,
    would still show ``(current directory)``.
    """

    def test_flags_a_default_fixed_at_import(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        demo = _demo_app(None, _import_time_queue_command)
        assert _defaults(_render_help(demo, ())) == ["(current directory)"]
        monkeypatch.chdir(tmp_path)
        parsed = _parsed_queue(typer.main.get_command(demo))
        assert parsed == _IMPORT_TIME_QUEUE_OPTION.default
        assert parsed != tmp_path.resolve()

    def test_passes_a_default_computed_at_run_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        command = typer.main.get_command(_demo_app(None, _queue_command))
        assert _parsed_queue(command) == tmp_path.resolve()

    def test_finds_the_queue_commands(self) -> None:
        # Guards the parametrised check below against a walk that finds nothing.
        assert {("supervisor", "status"), ("install",)} <= set(_queue_paths())

    @pytest.mark.parametrize("path", _queue_paths(), ids=_path_id)
    def test_defaults_to_the_directory_the_command_runs_in(
        self, path: tuple[str, ...], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CLI was built when this module was imported, in another directory.
        monkeypatch.chdir(tmp_path)
        command = _node(path)
        assert _queue_option(command).show_default == CWD_DEFAULT_LABEL
        assert _parsed_queue(command) == tmp_path.resolve()


class TestGroupHelp:
    """A group whose app has a callback prints the callback's docstring.

    Typer prints ``add_typer``'s ``help=`` in place of the docstring, so
    ``cli/__init__.py`` gives these groups ``short_help=``, the line the
    root listing prints, and no ``help=``.
    """

    def test_reads_the_page_of_a_callback_docstring(self) -> None:
        demo = _demo_group(short_help="One line.")
        docstring = inspect.getdoc(_demo_group_callback) or ""
        assert _group_docstrings(demo) == {("demo",): docstring}
        paragraphs = [
            "Summarise the demo group.",
            "Its second paragraph runs over two lines, which click joins and wraps again "
            "to fit the terminal.",
            "Exit codes: 0 clean 1 drift",
        ]
        assert _page_paragraphs(("demo",), demo) == paragraphs
        assert _one_line_paragraphs(docstring) == paragraphs

    def test_reads_the_page_of_an_add_typer_help(self) -> None:
        # The shape of the defect: typer puts add_typer's help= before the
        # callback's docstring.
        demo = _demo_group(help="One line.")
        assert _page_paragraphs(("demo",), demo) == ["One line."]
        assert _node(("demo",), typer.main.get_command(demo)).help == "One line."

    def test_finds_the_groups_with_callback_docstrings(self) -> None:
        # Guards the parametrised gate below against a walk that finds nothing.
        assert set(_group_docstrings()) == {
            ("doctor",),
            ("install",),
            ("install-skills",),
            ("usage",),
        }

    @pytest.mark.parametrize("path", sorted(_group_docstrings()), ids=_path_id)
    def test_page_prints_the_callback_docstring(self, path: tuple[str, ...]) -> None:
        docstring = _group_docstrings()[path]
        assert _node(path).help == docstring, (
            f"`claude-task-runner {_path_id(path)} --help` does not print the docstring "
            "of its app's callback. add_typer's help= replaces it; for the root "
            "listing's line, pass short_help= instead."
        )
        assert _page_paragraphs(path) == _one_line_paragraphs(docstring.partition("\f")[0])

    def test_usage_page_says_what_no_subcommand_does(self) -> None:
        assert _page_paragraphs(("usage",)) == [
            "Usage capture, parse, and drift check.",
            "With no subcommand, runs ``render``.",
        ]

    def test_root_listing_prints_the_short_help(self) -> None:
        short_help = {path: _node(path).short_help for path in _group_docstrings()}
        assert short_help == {
            ("doctor",): "Self-diagnostic battery (pass/warn/fail per check).",
            ("install",): (
                "Install the watchdog for one queue (systemd preferred, cron fallback). "
                "Installing it for another queue replaces the first."
            ),
            ("install-skills",): "Install the task-runner skills into ~/.claude/skills/.",
            ("usage",): "Usage capture, parse, and drift check.",
        }
        # The bracket and layout gates read these lines too.
        assert set(short_help.values()) <= set(_help_texts(CLI))
        listing = _squash(_render_help(app, ()))
        assert [text for text in short_help.values() if _squash(text) not in listing] == []


class TestListing:
    """The command listings that ``--help`` prints in an 80-column terminal."""

    def test_lays_help_out_as_a_terminal_does(self) -> None:
        # A terminal leaves two columns spare, up to 80 columns.
        assert {columns: _help_width(columns) for columns in (60, 80, 120)} == {
            60: 58,
            80: 78,
            120: 78,
        }

    def test_reads_a_listing(self) -> None:
        demo = _demo_listing()
        cut = "Summarise what this demo command does, in more words than its..."
        assert _listing(("demo",), demo) == {
            "long": cut,
            "short": "Summarise it briefly.",
            "whole": _DEMO_SHORT_HELP,
        }
        assert _cut_short(demo) == {("demo",): {"long": cut}}

    def test_rows_cut_short_today(self) -> None:
        assert _cut_short() == {
            (): {
                "account": "List configured accounts; pause/resume per-account...",
                "watchdog": "Watchdog tick (the cron entry-point) and queue...",
            },
            ("account",): {
                "list": "List configured accounts with their resolved policy and current...",
                "resume": "Reverse ``account pause <name>``; the dispatcher includes it...",
            },
            ("install",): {
                "uninstall": "Remove the watchdog installation (systemd unit AND/OR cron...",
            },
            ("install-skills",): {"list": "Show which task-runner skills are present in..."},
            ("queue",): {
                "backfill-working-dir": "Populate ``working_dir`` on tasks in ``todo/``...",
                "force-dispatch": "Bypass throttle and priority; dispatch...",
                "list": "List pending tasks in ``<queue>/todo/`` (Task...",
                "restart-fresh": "Clear a task's ``session_id`` so the next...",
                "template": "Print a complete, annotated example Task YAML --...",
            },
            ("usage",): {
                "refresh": "Refresh OAuth tokens for every configured account...",
                "whoami": "Show which Claude account this `[claude].config_dir` is...",
            },
            ("watchdog",): {
                "tick": "One watchdog tick: examine the queue the watchdog manages...",
            },
            ("worktree",): {
                "reclaim": "Reclaim the git worktrees of completed, merged, clean tasks...",
            },
        }


class TestConsoleMarkup:
    def test_console_print_still_renders_markup(self, tmp_path: Path) -> None:
        # rich_markup_mode governs help text only. `queue list` prints
        # "[dim]No pending tasks in todo/.[/]" through its own rich Console,
        # and the tags must still be consumed rather than printed.
        result = CliRunner().invoke(app, ["queue", "list", "--queue", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert _ANSI.sub("", result.output) == "No pending tasks in todo/.\n"
