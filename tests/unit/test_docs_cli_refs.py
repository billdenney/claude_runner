"""Docs must not document CLI commands the typer app does not define.

``docs/cheatsheet.md`` ended its "Add a new plan" recipe with
``claude-task-runner supervisor restart``. The ``supervisor`` group only
has ``start``, ``stop``, ``drain`` and ``status``, so an operator who
followed the step got ``No such command 'restart'``. The sweep after that
fix found the same defect in four more documents.
``docs/runbook.md`` and ``docs/first-time-setup.md`` both used
``queue list --status``, but ``--status`` lives on ``queue states``.
ADRs 0010 and 0014 described ``effort list``, ``config show`` and
``config validate`` subcommands that were never built.

``tests/unit/test_docs_config_refs.py`` guards config keys the same way.
This module walks every CLI invocation in the operator- and agent-facing
markdown down the real typer command tree. It fails on any command,
subcommand or option that the CLI would reject. It checks two shapes:

* ``claude-task-runner ...`` inside a code span or a fenced block.
* A code span that starts with a top-level group and a command word,
  such as ``sidecar answer``. The CHANGELOG and the ADRs mostly name
  commands this way. ADR-0030's unbuilt ``queue why-blocked`` is one
  example.

The package's own sources get the same walk, because operators and
agents copy commands from them too. The docstring of
``usage/oauth_refresh.py`` gave
``claude-task-runner usage refresh --queue ... --config ...``, but
``refresh`` takes no options, and ``--config`` belongs to the ``usage``
group, so it must come before the subcommand:

* Every shell script under ``src/claude_task_runner``: the skills'
  helpers and ``cron/watchdog.sh``. Shell code is checked like a fenced
  block. Comments, quoted strings and heredoc bodies are prose. A heredoc
  fed to ``python`` is scanned as Python.
* Every string literal, docstring and f-string in
  ``src/claude_task_runner/**/*.py``, as prose. So is every argv list
  such as ``["claude-task-runner", "sidecar", "show", ...]``.

Prose is checked the way markdown is: code spans and fences. A bare
``claude-task-runner`` in prose is checked too, but only when the next
word is a top-level group or an option, as in
``echo "run claude-task-runner supervisor restart"``. Any other next
word is English, as in ``claude-task-runner not on PATH``.

Last, the docstring of each CLI module must name every subcommand of
its group. ``cli/supervisor_cmd.py`` listed ``start | stop | status``
without ``drain``. ``cli/install_skills_cmd.py`` named an
``uninstall-skills`` command that does not exist; the real one is
``install-skills uninstall``.
"""

from __future__ import annotations

import ast
import importlib
import re
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import typer

from claude_task_runner.cli import app

REPO_ROOT = Path(__file__).parent.parent.parent
PACKAGE_DIR = REPO_ROOT / "src" / "claude_task_runner"
PROG = "claude-task-runner"

CLI: Any = typer.main.get_command(app)
"""The click command tree that ``claude-task-runner`` dispatches through.

Typed ``Any`` on purpose. Recent typer releases vendor click as
``typer._click``, while older ones depend on the ``click`` package. The
walker uses only the attributes both provide: ``commands``, ``params``,
``opts``, ``secondary_opts``, ``is_flag``, ``count`` and ``nargs``."""

DOCUMENTED_AS_ABSENT: dict[tuple[str, str], str] = {
    ("docs/first-time-setup.md", "config init"): (
        "Named only to say that the command does not exist and that this document replaces it."
    ),
    ("docs/decisions/0017-superseded-runner-cleanup.md", "config init"): (
        "Records that a README reference to this nonexistent subcommand was deleted."
    ),
    ("docs/decisions/0010-effort-levels-toml-driven.md", "effort list <model>"): (
        "ADRs are append-only. The 2026-09-25 update at the end records that "
        "the subcommand was never built and says what to use instead."
    ),
    ("docs/decisions/0014-all-cutoffs-as-settings.md", "config show"): (
        "ADRs are append-only. The 2026-09-25 update at the end records that "
        "the subcommand was never built and says what to use instead."
    ),
    ("docs/decisions/0014-all-cutoffs-as-settings.md", "config validate"): (
        "ADRs are append-only. The 2026-09-25 update at the end records that "
        "the subcommand was never built and says what to use instead."
    ),
    ("docs/decisions/0030-mechanical-readiness-gates.md", "queue why-blocked"): (
        "Named as a follow-up that was 'not built here'. The 2026-08-07 "
        "amendment records that it was never built."
    ),
}
"""``(repo-relative path, invocation)`` pairs that deliberately name a missing command.

The invocation is written without the ``claude-task-runner`` prefix, the
same way the failure report prints it. Add an entry ONLY for a doc (or a
script or source file) that says the command is absent or was never
built. A doc that tells an operator to *run* a missing command does not
belong here; that is the bug this test exists to catch. Each entry is
keyed to one file, so a new doc repeating the same invocation still
fails.
"""

_PROG_RE = re.compile(rf"(?<![\w.-]){re.escape(PROG)}(?![\w.-])")
"""``claude-task-runner`` as a word. It may follow a path such as
``~/.venv/bin/``, but it may not be part of a larger name such as
``claude-task-runner.service``."""

_COMMAND_WORD = re.compile(r"^[a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)*$")
"""A word in command position, such as ``drain``. It may also name
alternatives, as in ``pause/resume``. A placeholder (``<cmd>``) or a
value does not match, so it is treated as unverifiable, not as wrong."""

_CLOSERS = {"'": "'", '"': '"', "(": ")", "`": "`"}
"""When one of these characters directly precedes ``claude-task-runner``,
the invocation ends at the matching closer. Examples are
``Try 'claude-task-runner ... --help' for help`` and ``$(claude-task-runner ...)``."""

_FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.DOTALL)
"""An inline code span. It may wrap onto the next line, as in
``claude-task-runner sidecar`` / ``answer``, but it cannot cross a blank
line, because a blank line ends the paragraph."""

_HELP_OPTIONS = frozenset(CLI.context_settings.get("help_option_names", ["--help"]))

_OPTION_WORD = re.compile(r"^--?[a-z][a-z0-9-]*(?:=.*)?$")
"""An option such as ``--help``. As the first word after a bare
``claude-task-runner`` in prose, it marks an invocation, not English."""

_EXPR = "<expr>"
"""The word for a value only known at run time: an f-string replacement
field, or an argv element that is not a literal. It reads as a
placeholder, so the walk stops at it in command position and skips it
as an option value."""

_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_]\w*)\2")
"""A heredoc operator, such as ``<<'EOF'`` or ``<<-END``. A ``<<<``
here-string is ruled out before this is tried."""

_PYTHON = re.compile(r"(?<![\w.-])python[0-9.]*(?![\w.-])")
"""``python`` or ``python3`` as a word on the line that opens a heredoc."""


@dataclass(frozen=True)
class Mention:
    """One CLI invocation found in a doc."""

    lineno: int
    words: tuple[str, ...]
    """The tokens after ``claude-task-runner``, up to the end of the
    command: a shell operator, a comment, or the end of the span or line."""

    @property
    def invocation(self) -> str:
        return " ".join(self.words)


def _is_group(node: Any) -> bool:
    return isinstance(getattr(node, "commands", None), dict)


def _options(node: Any) -> dict[str, Any]:
    """Map every spelling of every option on ``node`` to its parameter.

    Both halves of a flag pair are included, so ``--wait`` and ``--no-wait``
    both resolve.
    """
    options: dict[str, Any] = {}
    for param in node.params:
        if param.param_type_name == "option":
            for name in (*param.opts, *param.secondary_opts):
                options[name] = param
    return options


def _check(words: Sequence[str]) -> str | None:
    """Walk ``words`` down the command tree and return why the CLI would reject them.

    Returns ``None`` when the words resolve. Returns ``None`` too once the
    walk reaches something it cannot verify, such as a placeholder where a
    command name should be, ``--help``, or ``--``. Checking stops at that
    point, but nothing is reported as wrong. Options are checked against
    the command they appear after, which is how click parses them. The
    value of an option that takes one is skipped, so a value such as
    ``--queue restart`` is never mistaken for a subcommand. Positional
    arguments of a leaf command are not checked.
    """
    node = CLI
    path = [PROG]
    i = 0
    while i < len(words):
        word = words[i]
        if word == "--" or word in _HELP_OPTIONS:
            return None
        if word.startswith("-") and word != "-":
            name = word.split("=", 1)[0]
            option = _options(node).get(name)
            if option is None:
                return f"no such option {name!r} on {' '.join(path)!r}"
            if "=" not in word and not (option.is_flag or option.count):
                i += option.nargs
        elif _is_group(node):
            if not _COMMAND_WORD.match(word):
                return None
            names = word.split("/")
            unknown = [n for n in names if n not in node.commands]
            if unknown:
                return (
                    f"no such command {unknown[0]!r} under {' '.join(path)!r} "
                    f"(it has: {', '.join(sorted(node.commands))})"
                )
            if len(names) > 1:
                return None
            node = node.commands[word]
            path.append(word)
        i += 1
    return None


def _is_shell_operator(token: str) -> bool:
    if token in {"|", "||", "&", "&&", ";", ")", "\\"}:
        return True
    if re.match(r"^\d*>", token):
        return True
    # ``< file`` and ``<<EOF`` are redirections. ``<task_id>`` is a
    # placeholder, and so is ``<queue>/claude_runner.toml``: a placeholder
    # with a path after it.
    return token.startswith("<") and not (
        token.endswith(">") or re.match(r"^<[A-Za-z_][\w-]*>", token)
    )


def _command_words(rest: str) -> tuple[str, ...]:
    """Tokenise the text after ``claude-task-runner`` into one command's words.

    Uses shell quoting rules, so a quoted JSON value stays one token and a
    ``# comment`` is dropped. The command ends at the first shell operator.
    """
    try:
        tokens = shlex.split(rest, comments=True)
    except ValueError:
        # The quote is unbalanced, which means the invocation sits inside a
        # quoted string that opened before it
        # (``echo "run claude-task-runner ..."``). That quote also closes
        # the command.
        tokens = []
        for raw in rest.split():
            if raw.startswith("#"):
                break
            token = raw.rstrip("'\"")
            if token:
                tokens.append(token)
            if token != raw:
                break
    words: list[str] = []
    for token in tokens:
        if _is_shell_operator(token):
            break
        word = token.rstrip(";)")
        if word:
            words.append(word)
        if word != token:
            break
    return tuple(words)


def _invocations(segment: str) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Yield ``(offset, words)`` for each ``claude-task-runner`` in ``segment``."""
    for match in _PROG_RE.finditer(segment):
        rest = segment[match.end() :]
        before = segment[match.start() - 1] if match.start() else ""
        closer = _CLOSERS.get(before)
        if closer is not None:
            rest = rest.split(closer, 1)[0]
        yield match.start(), _command_words(rest)


def _split_fences(text: str) -> tuple[list[tuple[int, str]], str]:
    """Split markdown into fenced command lines and the prose around them.

    Returns ``(fenced, prose)``. ``fenced`` holds each logical line inside
    a fence, paired with the number of its first physical line. Lines that
    end in ``\\`` are joined first. ``prose`` is the document with every
    fence blanked out, so line numbers still line up.
    """
    fenced: list[tuple[int, str]] = []
    prose: list[str] = []
    fence: str | None = None
    pending: list[str] = []
    start = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if fence is None:
            opener = _FENCE_OPEN.match(stripped)
            prose.append("" if opener else line)
            if opener:
                fence = opener.group(1)
            continue
        prose.append("")
        closing = stripped.startswith(fence) and set(stripped) == {fence[0]}
        if not closing:
            if not pending:
                start = lineno
            pending.append(stripped.removesuffix("\\"))
            if stripped.endswith("\\"):
                continue
        if pending:
            fenced.append((start, " ".join(pending)))
            pending = []
        if closing:
            fence = None
    if pending:
        fenced.append((start, " ".join(pending)))
    return fenced, "\n".join(prose)


def _iter_mentions(text: str) -> list[Mention]:
    """Return every CLI invocation in a markdown document, sorted by line.

    Fenced blocks are scanned in every language. The copy-paste path is
    where a wrong command does the most harm, and an unlabelled fence
    still holds shell. Prose is scanned only inside code spans, because
    words after a bare ``claude-task-runner`` in a sentence are English,
    not arguments.
    """
    fenced, prose = _split_fences(text)
    mentions = [
        Mention(lineno, words) for lineno, line in fenced for _, words in _invocations(line)
    ]
    for span in _CODE_SPAN.finditer(prose):
        body = span.group(2)
        invocations = list(_invocations(body))
        if not invocations:
            # A prefix-less `<group> <command>` span.
            words = _command_words(body)
            if len(words) >= 2 and words[0] in CLI.commands and _COMMAND_WORD.match(words[1]):
                invocations = [(0, words)]
        mentions.extend(
            Mention(prose.count("\n", 0, span.start(2) + offset) + 1, words)
            for offset, words in invocations
        )
    return sorted(mentions, key=lambda m: m.lineno)


def _prose_mentions(text: str) -> list[Mention]:
    """Return every CLI invocation in prose, such as a comment or a docstring.

    Everything :func:`_iter_mentions` finds counts, because prose can hold
    code spans and even fences. A bare ``claude-task-runner`` outside a
    code span counts too, but only when the next word is a top-level group
    or an option. Its words end with the line. Any other next word is
    English, as in ``claude-task-runner not on PATH``.
    """
    mentions = _iter_mentions(text)
    _, prose = _split_fences(text)
    bare = _CODE_SPAN.sub(lambda span: re.sub(r"[^\n]", " ", span.group()), prose)
    for lineno, line in enumerate(bare.split("\n"), 1):
        for _, words in _invocations(line):
            if words and (words[0] in CLI.commands or _OPTION_WORD.match(words[0])):
                mentions.append(Mention(lineno, words))
    return sorted(mentions, key=lambda m: m.lineno)


def _heredoc_end(text: str, start: int, delimiter: str, strip_tabs: bool) -> tuple[int, int]:
    """Find the end of the heredoc body that starts at offset ``start``.

    Returns ``(end, resume)``. ``end`` is where the terminator line starts,
    and ``resume`` is just past it. Raises ``ValueError`` when the
    terminator never appears, because a lexer that guessed wrong would
    otherwise turn the rest of the script into prose that is barely
    checked.
    """
    pos = start
    while pos < len(text):
        newline = text.find("\n", pos)
        stop = len(text) if newline < 0 else newline
        line = text[pos:stop]
        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
            return pos, stop + 1
        pos = stop + 1
    raise ValueError(f"heredoc <<{delimiter} is never terminated")


def _shell_kinds(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Label each character of a shell script as code or prose.

    Returns ``(kinds, python)``. ``kinds`` holds one label per character
    of ``text``. ``"c"`` is code. ``"p"`` is prose: a comment, the inside
    of a quoted string, or a heredoc body. ``"y"`` is the body of a heredoc
    fed to ``python``, and ``python`` holds the ``(start, end)`` offsets of
    each of those bodies. A ``$(...)`` or backtick substitution inside
    double quotes is code again. This is a small lexer, not a shell
    parser. It covers the constructs the packaged scripts use.
    """
    kinds = ["c"] * len(text)
    python: list[tuple[int, int]] = []
    stack: list[str] = []  # open quotes, parentheses and substitutions
    heredocs: list[tuple[str, bool, bool]] = []  # opened on the current line
    i = 0
    while i < len(text):
        char = text[i]
        top = stack[-1] if stack else ""
        if top == "'":
            if char == "'":
                stack.pop()
            else:
                kinds[i] = "p"
        elif top == '"':
            if char == '"':
                stack.pop()
            elif char == "`" or text.startswith("$(", i):
                stack.append("`" if char == "`" else "$(")
                i += len(stack[-1]) - 1
            else:
                kinds[i] = "p"
                if char == "\\" and i + 1 < len(text):
                    i += 1
                    kinds[i] = "p"
        elif char == "\\":
            i += 1  # an escaped character is never a quote or a comment
        elif char == "#" and (i == 0 or text[i - 1] in " \t\n;&|()"):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            kinds[i:end] = ["p"] * (end - i)
            i = end
            continue
        elif char in "'\"(" or text.startswith("$(", i):
            stack.append("$(" if char == "$" else char)
            i += len(stack[-1]) - 1
        elif char == ")" and top in ("(", "$("):
            stack.pop()
        elif char == "`":
            if top == "`":
                stack.pop()
            else:
                stack.append("`")
        elif text.startswith("<<<", i):
            i += 2  # a here-string, not a heredoc
        elif heredoc := _HEREDOC.match(text, i):
            line = text[text.rfind("\n", 0, i) + 1 : i]
            heredocs.append((heredoc[3], heredoc[1] == "-", bool(_PYTHON.search(line))))
            i = heredoc.end()
            continue
        elif char == "\n" and heredocs:
            i += 1
            for delimiter, strip_tabs, is_python in heredocs:
                end, resume = _heredoc_end(text, i, delimiter, strip_tabs)
                kinds[i:end] = ["y" if is_python else "p"] * (end - i)
                if is_python:
                    python.append((i, end))
                i = resume
            heredocs.clear()
            continue
        i += 1
    return kinds, python


def _shell_mentions(text: str) -> list[Mention]:
    """Return every CLI invocation in a shell script, sorted by line.

    Code is scanned the way a fenced block is: every
    ``claude-task-runner`` in code is an invocation, and its words run to
    the end of the logical line. Prose goes through
    :func:`_prose_mentions`, and each Python heredoc through
    :func:`_python_mentions`.
    """
    kinds, python = _shell_kinds(text)
    mentions: list[Mention] = []
    start = 0
    # Joining continuation lines with two spaces keeps every offset in place.
    for line in text.replace("\\\n", "  ").split("\n"):
        for offset, words in _invocations(line):
            if kinds[start + offset] == "c":
                mentions.append(Mention(text.count("\n", 0, start + offset) + 1, words))
        start += len(line) + 1
    prose = "".join(
        char if kind == "p" or char == "\n" else " " for char, kind in zip(text, kinds, strict=True)
    )
    mentions.extend(_prose_mentions(prose))
    for body_start, body_end in python:
        opener = text.count("\n", 0, body_start)
        try:
            found = _python_mentions(text[body_start:body_end])
        except SyntaxError as exc:
            raise ValueError(f"the python heredoc opened on line {opener} does not parse") from exc
        mentions.extend(Mention(opener + m.lineno, m.words) for m in found)
    return sorted(mentions, key=lambda m: m.lineno)


def _literal_or_expr(node: ast.AST) -> str:
    """Return a str literal's value, or :data:`_EXPR` for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return _EXPR


def _python_mentions(source: str) -> list[Mention]:
    """Return every CLI invocation in Python source, sorted by line.

    Every string literal, docstring and f-string is prose (see
    :func:`_prose_mentions`), and an f-string's replacement fields read as
    :data:`_EXPR`. A mention's line is the string's first line plus the
    newlines before the mention in the string's value. That is exact for
    a docstring. For a string built from several literals, it can point
    at an earlier line of the same string. A list or tuple whose first
    element is the literal ``claude-task-runner`` is an argv vector: each
    literal element is a word, and any other element reads as
    :data:`_EXPR`. Comments are not scanned.
    """
    tree = ast.parse(source)
    fstring_parts = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    mentions: list[Mention] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List | ast.Tuple) and node.elts:
            head = _literal_or_expr(node.elts[0])
            if head == PROG or head.endswith(f"/{PROG}"):
                words = tuple(_literal_or_expr(element) for element in node.elts[1:])
                mentions.append(Mention(node.lineno, words))
            continue
        if isinstance(node, ast.JoinedStr):
            value = "".join(_literal_or_expr(part) for part in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in fstring_parts:
                continue
            value = node.value
        else:
            continue
        last = node.end_lineno or node.lineno
        mentions.extend(
            Mention(min(node.lineno + m.lineno - 1, last), m.words) for m in _prose_mentions(value)
        )
    return sorted(mentions, key=lambda m: m.lineno)


def _doc_files() -> list[Path]:
    """The operator- and agent-facing markdown that names CLI commands."""
    return [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CHANGELOG.md",
        *sorted((REPO_ROOT / "docs").rglob("*.md")),
        *sorted((REPO_ROOT / "src" / "claude_task_runner" / "skills").glob("*/SKILL.md")),
    ]


def _script_files() -> list[Path]:
    """Every shell script the package ships: the skills' helpers and the cron watchdog."""
    return sorted(PACKAGE_DIR.rglob("*.sh"))


def _python_files() -> list[Path]:
    """Every Python module the package ships, the skills' helper scripts included."""
    return sorted(PACKAGE_DIR.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _mentions_in(path: Path) -> list[Mention]:
    """Scan ``path`` with the scanner for its type of file."""
    scanners = {".md": _iter_mentions, ".sh": _shell_mentions, ".py": _python_mentions}
    return scanners[path.suffix](path.read_text())


def _rejected(path: Path) -> list[str]:
    """Report each invocation in ``path`` that the CLI rejects and that is not allowlisted."""
    rel = _rel(path)
    return [
        f"  {rel}:{mention.lineno}: `{PROG} {mention.invocation}` -- {problem}"
        for mention in _mentions_in(path)
        if (problem := _check(mention.words)) is not None
        and (rel, mention.invocation) not in DOCUMENTED_AS_ABSENT
    ]


def _group_names() -> list[str]:
    return sorted(name for name, node in CLI.commands.items() if _is_group(node))


def _group_modules() -> dict[str, ModuleType]:
    """Map each top-level group to the module that defines its commands."""
    modules: dict[str, ModuleType] = {}
    for info in app.registered_groups:
        group = info.typer_instance
        assert group is not None and info.name is not None
        callbacks = [command.callback for command in group.registered_commands]
        if group.registered_callback is not None:
            callbacks.append(group.registered_callback.callback)
        # Unpacking fails loudly if a group's commands span several modules.
        (name,) = {callback.__module__ for callback in callbacks if callback is not None}
        modules[info.name] = importlib.import_module(name)
    return modules


def _listed_subcommands(doc: str, group: str) -> set[str]:
    """Return the subcommands of ``group`` that a docstring names in code spans.

    Each span loses a leading ``claude-task-runner`` and then a leading
    group name. The first word left names a subcommand, and so does each
    word after a ``|``. A word such as ``pause/resume`` names both. So
    ``queue show ID``, ``show`` and ``claude-task-runner supervisor start |
    stop`` all count.
    """
    names: set[str] = set()
    for span in _CODE_SPAN.finditer(doc):
        words = span.group(2).split()
        if words[:1] == [PROG]:
            words = words[1:]
        if words[:1] == [group]:
            words = words[1:]
        for i, word in enumerate(words):
            if i == 0 or words[i - 1] == "|":
                names.update(word.split("/"))
    return names


def _command_paths(node: Any = CLI, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every command path in the tree, including groups and the root ``()``."""
    paths = [prefix]
    if _is_group(node):
        for name, sub in sorted(node.commands.items()):
            paths.extend(_command_paths(sub, (*prefix, name)))
    return paths


def _node(path: tuple[str, ...]) -> Any:
    node = CLI
    for name in path:
        node = node.commands[name]
    return node


class TestCliWalker:
    """The walker itself. A broken checker would pass everything."""

    def test_rejects_fake_subcommand(self) -> None:
        # The original bug: `supervisor restart` does not exist.
        problem = _check(("supervisor", "restart"))
        assert problem == (
            "no such command 'restart' under 'claude-task-runner supervisor' "
            "(it has: drain, start, status, stop)"
        )

    def test_rejects_fake_top_level_group(self) -> None:
        assert _check(("config", "init")) == (
            "no such command 'config' under 'claude-task-runner' (it has: account, "
            "doctor, install, install-skills, queue, sidecar, supervisor, usage, watchdog, "
            "worktree)"
        )

    def test_rejects_fake_option(self) -> None:
        # `--status` lives on `queue states`, not `queue list`.
        assert _check(("queue", "list", "--status", "awaiting_sidecar")) == (
            "no such option '--status' on 'claude-task-runner queue list'"
        )
        assert _check(("queue", "states", "--status", "awaiting_sidecar")) is None

    def test_rejects_group_option_after_its_subcommand(self) -> None:
        # click binds `--config` to `usage` only when it precedes the
        # subcommand, so `usage whoami --config x` fails at the shell too.
        assert _check(("usage", "--config", "q.toml", "whoami")) is None
        assert _check(("usage", "whoami", "--config", "q.toml")) == (
            "no such option '--config' on 'claude-task-runner usage whoami'"
        )

    def test_option_value_is_not_resolved_as_a_command(self) -> None:
        # The value of --queue happens to be spelled like a (fake) subcommand.
        assert _check(("supervisor", "start", "--queue", "restart")) is None

    def test_accepts_equals_form_and_flag_pair_halves(self) -> None:
        assert _check(("queue", "states", "--status=running")) is None
        assert _check(("supervisor", "drain", "--no-wait")) is None
        assert _check(("usage", "-c", "q.toml")) is None

    def test_accepts_positional_arguments_of_a_leaf(self) -> None:
        assert _check(("sidecar", "answer", "<task_id>", "<seq>", "--merge")) is None

    def test_accepts_bare_groups_and_root(self) -> None:
        assert _check(()) is None
        assert _check(("supervisor",)) is None
        assert _check(("doctor", "--json")) is None

    def test_alternatives_are_each_checked(self) -> None:
        assert _check(("account", "pause/resume")) is None
        assert _check(("account", "pause/restart")) == (
            "no such command 'restart' under 'claude-task-runner account' "
            "(it has: list, pause, resume)"
        )

    def test_unverifiable_tokens_stop_the_walk(self) -> None:
        assert _check(("<group>", "<command>")) is None
        assert _check(("queue", "--help")) is None
        assert _check(("queue", "add", "--", "--not-an-option")) is None

    @pytest.mark.parametrize("path", _command_paths(), ids=lambda p: " ".join(p) or "<root>")
    def test_accepts_every_real_command_and_option(self, path: tuple[str, ...]) -> None:
        # This enumerates the whole tree, so a walker bug that rejects a
        # real command or option cannot hide behind the examples above.
        assert _check(path) is None
        for name, option in _options(_node(path)).items():
            value = () if option.is_flag or option.count else ("VALUE",) * option.nargs
            assert _check((*path, name, *value)) is None, name

    @pytest.mark.parametrize(
        "path",
        [p for p in _command_paths() if _is_group(_node(p))],
        ids=lambda p: " ".join(p) or "<root>",
    )
    def test_rejects_unknown_name_under_every_group(self, path: tuple[str, ...]) -> None:
        where = " ".join((PROG, *path))
        problem = _check((*path, "no-such-command"))
        assert problem is not None
        assert problem.startswith(f"no such command 'no-such-command' under {where!r} (it has: ")
        assert _check((*path, "--no-such-option")) == (
            f"no such option '--no-such-option' on {where!r}"
        )

    def test_no_group_takes_a_positional_argument(self) -> None:
        # The walker treats a bare word after a group as a subcommand name.
        # If a group ever declared a positional argument, click would bind
        # the word to that argument instead, and _check would report a real
        # command as missing.
        for path in _command_paths():
            node = _node(path)
            if _is_group(node):
                args = [p.name for p in node.params if p.param_type_name == "argument"]
                assert not args, f"{' '.join((PROG, *path))} takes {args}"


class TestInvocationExtraction:
    def test_extracts_code_span(self) -> None:
        assert _iter_mentions("run `claude-task-runner supervisor drain --no-wait` now") == [
            Mention(1, ("supervisor", "drain", "--no-wait"))
        ]

    def test_extracts_code_span_wrapped_across_lines(self) -> None:
        text = "a batch of `claude-task-runner sidecar\nanswer` calls"
        assert _iter_mentions(text) == [Mention(1, ("sidecar", "answer"))]

    def test_extracts_fence_with_continuations_and_comment(self) -> None:
        text = (
            "intro\n\n"
            "```sh\n"
            "claude-task-runner queue add \\\n"
            "    --id 007 --title 'two words'   # trailing comment\n"
            "claude-task-runner supervisor drain | tee log\n"
            "```\n"
        )
        assert _iter_mentions(text) == [
            Mention(4, ("queue", "add", "--id", "007", "--title", "two words")),
            Mention(6, ("supervisor", "drain")),
        ]

    def test_scans_indented_and_unlabelled_fences(self) -> None:
        text = "1. step\n\n   ```\n   claude-task-runner supervisor restart\n   ```\n"
        assert _iter_mentions(text) == [Mention(4, ("supervisor", "restart"))]

    def test_quote_before_program_closes_the_invocation(self) -> None:
        # Quoted from a click error message in the CHANGELOG.
        text = "```\nTry 'claude-task-runner supervisor drain --help' for help.\n```"
        assert _iter_mentions(text) == [Mention(2, ("supervisor", "drain", "--help"))]

    def test_unbalanced_quote_ends_the_command(self) -> None:
        text = '```sh\necho "then claude-task-runner supervisor restart" >> notes\n```'
        (mention,) = _iter_mentions(text)
        assert mention.words == ("supervisor", "restart")
        assert _check(mention.words) is not None

    def test_extracts_group_led_span_without_prefix(self) -> None:
        text = "the `queue why-blocked` command; see `sidecar list --json`"
        assert _iter_mentions(text) == [
            Mention(1, ("queue", "why-blocked")),
            Mention(1, ("sidecar", "list", "--json")),
        ]

    def test_ignores_non_command_shapes(self) -> None:
        text = (
            "# claude-task-runner\n"
            "prose claude-task-runner supervisor restart outside backticks\n"
            "`systemctl --user edit claude-task-runner.service` and "
            "`~/.local/share/claude-task-runner`\n"
            "`install` alone, `queue/todo/` path, `supervisor.json` file\n"
        )
        mentions = _iter_mentions(text)
        assert mentions == [Mention(3, ())]
        assert _check(mentions[0].words) is None

    def test_placeholder_is_not_a_redirection(self) -> None:
        assert _command_words("queue show <task_id> --json > out.json") == (
            "queue",
            "show",
            "<task_id>",
            "--json",
        )

    def test_placeholder_with_a_path_is_not_a_redirection(self) -> None:
        # The corrected usage/oauth_refresh.py docstring. Read as `<` plus
        # a file, the walk stopped at --config and never checked `refresh`.
        assert _command_words("usage --config <queue>/claude_runner.toml refresh") == (
            "usage",
            "--config",
            "<queue>/claude_runner.toml",
            "refresh",
        )

    @pytest.mark.parametrize("redirection", ["< in.txt", "<in.txt", "<<EOF", "<(cat x)"])
    def test_redirections_still_end_the_command(self, redirection: str) -> None:
        assert _command_words(f"queue add {redirection} --id 1") == ("queue", "add")


class TestProseExtraction:
    """Comments, quoted strings and docstrings: code spans plus group-led bare mentions."""

    def test_group_led_bare_mention_is_checked(self) -> None:
        (mention,) = _prose_mentions("then run claude-task-runner supervisor restart")
        assert mention == Mention(1, ("supervisor", "restart"))
        assert _check(mention.words) == (
            "no such command 'restart' under 'claude-task-runner supervisor' "
            "(it has: drain, start, status, stop)"
        )

    def test_option_led_bare_mention_is_checked(self) -> None:
        (mention,) = _prose_mentions("claude-task-runner --version prints it")
        assert _check(mention.words) == "no such option '--version' on 'claude-task-runner'"

    @pytest.mark.parametrize(
        "english",
        [
            "claude-task-runner not on PATH",
            "Exits non-zero if claude-task-runner is missing or list/show errors.",
            "# claude-task-runner Task YAML -- save as <queue>/todo/<id>.yaml",
            "/etc/sudoers.d/claude-task-runner:",
        ],
    )
    def test_english_after_a_bare_mention_is_skipped(self, english: str) -> None:
        # All four are real text from the packaged sources.
        assert _prose_mentions(english) == []

    def test_code_span_is_checked_as_in_markdown(self) -> None:
        # The usage/oauth_refresh.py docstring before this fix.
        text = "* As an operator preflight:\n  ``claude-task-runner usage refresh --queue ... --config ...``"
        (mention,) = _prose_mentions(text)
        assert mention.lineno == 2
        assert _check(mention.words) == (
            "no such option '--queue' on 'claude-task-runner usage refresh'"
        )

    def test_bare_mention_ends_with_its_line(self) -> None:
        text = "run claude-task-runner supervisor\nrestart it later"
        assert _prose_mentions(text) == [Mention(1, ("supervisor",))]

    def test_span_and_bare_text_are_not_double_counted(self) -> None:
        text = "use `claude-task-runner queue list` or claude-task-runner queue states"
        assert _prose_mentions(text) == [
            Mention(1, ("queue", "list")),
            Mention(1, ("queue", "states")),
        ]


class TestShellExtraction:
    def test_code_is_checked_word_by_word(self) -> None:
        text = 'set -e\nclaude-task-runner sidecar list --queue "$QUEUE" --json > "$OUT"\n'
        assert _shell_mentions(text) == [
            Mention(2, ("sidecar", "list", "--queue", "$QUEUE", "--json"))
        ]

    def test_bare_program_in_code_is_an_empty_invocation(self) -> None:
        text = "if ! command -v claude-task-runner >/dev/null 2>&1; then exit 1; fi\n"
        assert _shell_mentions(text) == [Mention(1, ())]

    def test_continuation_lines_are_joined(self) -> None:
        text = "claude-task-runner queue add \\\n    --id 007 --bogus\n"
        (mention,) = _shell_mentions(text)
        assert mention == Mention(1, ("queue", "add", "--id", "007", "--bogus"))
        assert _check(mention.words) == "no such option '--bogus' on 'claude-task-runner queue add'"

    def test_comment_is_prose(self) -> None:
        text = (
            "# Exits non-zero if claude-task-runner is missing or list/show errors.\n"
            "# Delegates to `claude-task-runner supervisor restart`.\n"
        )
        assert _shell_mentions(text) == [Mention(2, ("supervisor", "restart"))]

    def test_quoted_string_is_prose(self) -> None:
        text = (
            'echo "$(date -u) watchdog: claude-task-runner not found on PATH" >> "$LOG"\n'
            "echo 'then run claude-task-runner supervisor restart'\n"
        )
        assert _shell_mentions(text) == [Mention(2, ("supervisor", "restart"))]

    def test_substitution_inside_double_quotes_is_code(self) -> None:
        text = 'echo "open: $(claude-task-runner sidecar lst --json | wc -l)"\n'
        (mention,) = _shell_mentions(text)
        assert mention == Mention(1, ("sidecar", "lst", "--json"))
        assert _check(mention.words) is not None

    def test_hash_inside_a_word_is_not_a_comment(self) -> None:
        text = "[[ $# -gt 0 ]] && n=${#arr[@]} && claude-task-runner watchdog tik\n"
        assert _shell_mentions(text) == [Mention(1, ("watchdog", "tik"))]

    def test_python_heredoc_is_scanned_as_python(self) -> None:
        text = (
            "QUEUE=$QUEUE python3 - <<'PY'\n"
            "import subprocess\n"
            'subprocess.run(["claude-task-runner", "sidecar", "show", tid, "--jsn"])\n'
            'print("claude-task-runner not on PATH")\n'
            "PY\n"
            "claude-task-runner watchdog tick\n"
        )
        mentions = _shell_mentions(text)
        assert mentions == [
            Mention(3, ("sidecar", "show", _EXPR, "--jsn")),
            Mention(6, ("watchdog", "tick")),
        ]
        assert _check(mentions[0].words) == (
            "no such option '--jsn' on 'claude-task-runner sidecar show'"
        )

    def test_other_heredoc_is_prose(self) -> None:
        text = (
            "cat <<-EOF\n"
            "\tRun claude-task-runner supervisor restart to pick it up.\n"
            "\tclaude-task-runner not on PATH\n"
            "\tEOF\n"
            "claude-task-runner watchdog tick\n"
        )
        assert _shell_mentions(text) == [
            Mention(2, ("supervisor", "restart", "to", "pick", "it", "up.")),
            Mention(5, ("watchdog", "tick")),
        ]

    def test_here_string_is_not_a_heredoc(self) -> None:
        text = 'read -r -a brs <<< "$line"\nclaude-task-runner watchdog tick\n'
        assert _shell_mentions(text) == [Mention(2, ("watchdog", "tick"))]

    def test_unterminated_heredoc_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="heredoc <<EOF is never terminated"):
            _shell_mentions("cat <<EOF\nno terminator\n")

    def test_unparseable_python_heredoc_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="python heredoc opened on line 2 does not parse"):
            _shell_mentions("set -e\npython3 - <<'PY'\nprint(\nPY\n")


class TestPythonExtraction:
    def test_docstring_mention_has_its_source_line(self) -> None:
        source = '"""Usage.\n\nRun ``claude-task-runner usage refresh --queue x``.\n"""\n'
        (mention,) = _python_mentions(source)
        assert mention == Mention(3, ("usage", "refresh", "--queue", "x"))

    def test_fstring_field_reads_as_a_placeholder(self) -> None:
        source = 'msg = f"run `claude-task-runner install --queue {queue_dir}` first"\n'
        (mention,) = _python_mentions(source)
        assert mention == Mention(1, ("install", "--queue", _EXPR))
        assert _check(mention.words) is None

    def test_fstring_parts_are_not_scanned_twice(self) -> None:
        source = 'msg = f"run `claude-task-runner supervisor start` for {q}"\n'
        assert _python_mentions(source) == [Mention(1, ("supervisor", "start"))]

    def test_concatenated_literals_are_one_string(self) -> None:
        source = 'msg = (\n    "then run claude-task-runner "\n    "supervisor restart"\n)\n'
        assert _python_mentions(source) == [Mention(2, ("supervisor", "restart"))]

    def test_argv_list_and_tuple_are_invocations(self) -> None:
        source = (
            'subprocess.run(["claude-task-runner", "sidecar", "show", tid, "--json"])\n'
            'CMD = ("/opt/venv/bin/claude-task-runner", "supervisor", "restart")\n'
            'OTHER = ["git", "claude-task-runner", "status"]\n'
        )
        mentions = _python_mentions(source)
        assert mentions == [
            Mention(1, ("sidecar", "show", _EXPR, "--json")),
            Mention(2, ("supervisor", "restart")),
        ]
        assert _check(mentions[0].words) is None
        assert _check(mentions[1].words) is not None

    def test_english_in_an_error_message_is_skipped(self) -> None:
        source = 'raise RuntimeError("claude-task-runner not on PATH")\n'
        assert _python_mentions(source) == []


class TestDocsMatchCli:
    def test_doc_sources_are_present(self) -> None:
        # Guards the whole suite. A wrong root would make every parametrised
        # case vanish, and the gate would pass on 0 files.
        files = _doc_files()
        assert all(f.is_file() for f in files), [str(f) for f in files if not f.is_file()]
        assert any(_rel(f).startswith("docs/") for f in files)
        assert any(f.name == "SKILL.md" for f in files)

    def test_scan_finds_a_known_invocation(self) -> None:
        # Guards against an extractor that silently finds nothing. The
        # README quick start ends by starting the supervisor.
        mentions = _iter_mentions((REPO_ROOT / "README.md").read_text())
        assert ("supervisor", "start") in {m.words for m in mentions}

    @pytest.mark.parametrize("doc", _doc_files(), ids=_rel)
    def test_every_cli_reference_exists(self, doc: Path) -> None:
        bad = _rejected(doc)
        assert not bad, (
            "CLI invocation(s) in docs that claude-task-runner does not accept.\n"
            "An operator who follows these gets 'No such command' or "
            "'No such option'.\n"
            + "\n".join(bad)
            + "\nFix the docs, or add the command to the CLI. Add "
            "(path, invocation) to DOCUMENTED_AS_ABSENT in this file only "
            "when the doc says the command is absent or was never built."
        )

    @pytest.mark.parametrize(("rel", "invocation"), sorted(DOCUMENTED_AS_ABSENT))
    def test_allowlist_entry_is_still_needed(self, rel: str, invocation: str) -> None:
        # A stale entry would quietly allow the command to be documented as
        # real in that file again. This fails once the doc stops naming the
        # command, or once the command is built, so the entry gets deleted.
        mentions = _mentions_in(REPO_ROOT / rel)
        rejected = {m.invocation for m in mentions if _check(m.words) is not None}
        assert invocation in rejected, (
            f"DOCUMENTED_AS_ABSENT[({rel!r}, {invocation!r})] no longer matches a "
            "rejected invocation in that file; delete the entry."
        )


class TestSourcesMatchCli:
    def test_sources_are_present(self) -> None:
        # Guards the parametrised cases below. A wrong root would make them
        # vanish, and the gate would pass on 0 files.
        scripts = {_rel(f) for f in _script_files()}
        assert "src/claude_task_runner/cron/watchdog.sh" in scripts
        assert "src/claude_task_runner/skills/runner-answer-sidecar/fetch_all.sh" in scripts
        modules = {_rel(f) for f in _python_files()}
        assert "src/claude_task_runner/usage/oauth_refresh.py" in modules
        assert "src/claude_task_runner/cli/supervisor_cmd.py" in modules

    def test_scan_finds_the_watchdog_invocations(self) -> None:
        # Guards against a shell scanner that silently finds nothing: the five
        # comment spans, `command -v`, and the real call, in order.
        assert _mentions_in(PACKAGE_DIR / "cron" / "watchdog.sh") == [
            Mention(3, ("install",)),
            Mention(6, ("watchdog", "tick")),
            Mention(9, ("watchdog", "register")),
            Mention(10, ("watchdog", "unregister")),
            Mention(16, ("supervisor", "start", "--queue", "...")),
            Mention(35, ()),
            Mention(40, ("watchdog", "tick")),
        ]

    def test_scan_finds_the_fetch_all_invocations(self) -> None:
        # A comment span, the `sidecar list` call, a span in a docstring of
        # the Python heredoc, and the argv list that heredoc runs
        # `sidecar show` with.
        fetch_all = PACKAGE_DIR / "skills" / "runner-answer-sidecar" / "fetch_all.sh"
        assert _mentions_in(fetch_all) == [
            Mention(29, ("sidecar", "answer")),
            Mention(49, ("sidecar", "list", "--queue", "$QUEUE", "--json")),
            Mention(71, ("sidecar", "list")),
            Mention(95, ("sidecar", "show", _EXPR, _EXPR, "--queue", _EXPR, "--json")),
        ]

    def test_scan_finds_the_fixed_oauth_refresh_docstring(self) -> None:
        mentions = _mentions_in(PACKAGE_DIR / "usage" / "oauth_refresh.py")
        assert mentions == [
            Mention(24, ("usage", "--config", "<queue>/claude_runner.toml", "refresh"))
        ]

    @pytest.mark.parametrize("script", _script_files(), ids=_rel)
    def test_every_script_cli_reference_exists(self, script: Path) -> None:
        bad = _rejected(script)
        assert not bad, (
            "CLI invocation(s) in a packaged shell script that claude-task-runner "
            "does not accept.\n"
            + "\n".join(bad)
            + "\nFix the script, or add the command to the CLI. Shell code is "
            "checked word by word, so quote an echo argument that is English."
        )

    @pytest.mark.parametrize("module", _python_files(), ids=_rel)
    def test_every_python_cli_reference_exists(self, module: Path) -> None:
        bad = _rejected(module)
        assert not bad, (
            "CLI invocation(s) in a Python string or docstring that "
            "claude-task-runner does not accept.\n"
            + "\n".join(bad)
            + "\nFix the text, or add the command to the CLI. Add (path, "
            "invocation) to DOCUMENTED_AS_ABSENT in this file only when the "
            "text says the command is absent or was never built."
        )


class TestCliModuleDocstrings:
    """Each CLI module's docstring must name every subcommand of its group."""

    def test_listed_subcommands_known_answers(self) -> None:
        # The supervisor_cmd.py and install_skills_cmd.py docstrings before
        # this fix, then the other shapes the docstrings use.
        old_supervisor = "``claude-task-runner supervisor start | stop | status`` subcommands."
        assert _listed_subcommands(old_supervisor, "supervisor") == {"start", "stop", "status"}
        old_skills = "``claude-task-runner install-skills`` and ``uninstall-skills``."
        assert _listed_subcommands(old_skills, "install-skills") == {"uninstall-skills"}
        assert _listed_subcommands("* ``queue show ID`` — show one task.", "queue") == {"show"}
        assert _listed_subcommands("``account pause/resume``", "account") == {"pause", "resume"}
        assert _listed_subcommands("the ``watchdog`` group runs ``tick``", "watchdog") == {"tick"}

    def test_old_docstrings_would_fail(self) -> None:
        # A gate that could not see the original omissions would pass anything.
        supervisor = set(CLI.commands["supervisor"].commands)
        old = "``claude-task-runner supervisor start | stop | status`` subcommands."
        assert supervisor - _listed_subcommands(old, "supervisor") == {"drain"}
        skills = set(CLI.commands["install-skills"].commands)
        old = "``claude-task-runner install-skills`` and ``uninstall-skills``."
        assert skills - _listed_subcommands(old, "install-skills") == {"list", "uninstall"}

    def test_every_group_maps_to_a_module(self) -> None:
        # Guards the parametrised case below against a mapping that finds
        # nothing, and pins the one-module-per-group layout it assumes.
        modules = _group_modules()
        assert sorted(modules) == _group_names()
        assert modules["supervisor"].__name__ == "claude_task_runner.cli.supervisor_cmd"

    @pytest.mark.parametrize("group", _group_names())
    def test_module_docstring_names_every_subcommand(self, group: str) -> None:
        module = _group_modules()[group]
        assert module.__doc__, f"{module.__name__} has no module docstring"
        commands = set(CLI.commands[group].commands)
        missing = sorted(commands - _listed_subcommands(module.__doc__, group))
        assert not missing, (
            f"The {module.__name__} docstring does not name {missing}, which "
            f"`{PROG} {group}` has. Name each subcommand in a code span, such "
            f"as ``{group} {missing[0]}``."
        )
