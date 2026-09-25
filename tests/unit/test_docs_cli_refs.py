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
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import typer

from claude_task_runner.cli import app

REPO_ROOT = Path(__file__).parent.parent.parent
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
same way the failure report prints it. Add an entry ONLY for a doc that
says the command is absent or was never built. A doc that tells an
operator to *run* a missing command does not belong here; that is the
bug this test exists to catch. Each entry is keyed to one file, so a new
doc repeating the same invocation still fails.
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
    # ``< file`` and ``<<EOF`` are redirections; ``<task_id>`` is a placeholder.
    return token.startswith("<") and not token.endswith(">")


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


def _doc_files() -> list[Path]:
    """The operator- and agent-facing markdown that names CLI commands."""
    return [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CHANGELOG.md",
        *sorted((REPO_ROOT / "docs").rglob("*.md")),
        *sorted((REPO_ROOT / "src" / "claude_task_runner" / "skills").glob("*/SKILL.md")),
    ]


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


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
            "doctor, install, install-skills, queue, sidecar, supervisor, usage, watchdog)"
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
        bad: list[str] = []
        for mention in _iter_mentions(doc.read_text()):
            problem = _check(mention.words)
            if problem is None or (_rel(doc), mention.invocation) in DOCUMENTED_AS_ABSENT:
                continue
            bad.append(
                f"  {_rel(doc)}:{mention.lineno}: `{PROG} {mention.invocation}` -- {problem}"
            )
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
        mentions = _iter_mentions((REPO_ROOT / rel).read_text())
        rejected = {m.invocation for m in mentions if _check(m.words) is not None}
        assert invocation in rejected, (
            f"DOCUMENTED_AS_ABSENT[({rel!r}, {invocation!r})] no longer matches a "
            "rejected invocation in that file; delete the entry."
        )
