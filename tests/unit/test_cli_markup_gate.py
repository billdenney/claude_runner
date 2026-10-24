"""No print in the package hands free text to Rich as markup.

Rich reads markup (``[bold]``, ``[/]``) and emoji codes (``:b:``) in every
``str`` it is given to render: by ``Console.print``, ``log``, ``input``,
``rule`` and ``status``, ``Table.add_row`` and ``add_column``, a prompt
(``Prompt``, ``Confirm``, ``IntPrompt``, ``FloatPrompt``, built or asked),
and ``rich.print``. A task id,
path, sidecar answer, account error or exception message holding either
would be restyled, cut short, turned into a symbol, or raise
``MarkupError``. So every value from outside the code reaches a print
inside a ``Text``, built with :func:`claude_task_runner.cli._helpers.literal`
(or ``styled``, ``Text`` or ``Text.assemble``), or the print turns both off
(``markup=False, emoji=False``).

This gate reads the source: each argument rendered must be a string
constant, a ``Text`` line, a conditional expression choosing between such,
or printed with markup and emoji off. A console is any name holding
``console``, bound to ``Console(...)`` (``Console`` imported under any
name) or annotated ``Console``; a name or attribute bound to its ``print``
or ``log`` (``self.say = console.print``), or Rich's ``print`` imported
under any name, prints too. :data:`ALLOWLIST` may name a printed expression
proven to hold no free text; it must stay exact. The tests at the bottom
show the gate fails on each way a site can be reintroduced.

It reads one module at a time, by name, and does not follow values. It
does not see a console's ``log``, ``input``, ``rule`` or ``status`` on a
parameter neither named for a console nor annotated ``Console`` (its
``print`` it does see, on any receiver), a method reached through
``functools.partial``, ``getattr`` or a container, or Rich renderables it
does not list (``Panel``, ``Table(title=...)``, ``Status.update``,
``Progress`` descriptions). None is used in the package; a new use needs
the gate extended, or a test of its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "claude_task_runner"

SAFE_LINES = frozenset({"literal", "styled", "Text"})
"""Calls whose result is a ``Text``, which Rich prints as it is; so is
``Text.assemble``, whose ``str`` parts are plain text too."""

CONSOLE_PRINTS = frozenset({"print", "log"})
"""``Console`` methods that render every positional argument. ``print`` is
matched on any receiver but ``typer``; ``log`` only on a console's."""

CONSOLE_PROMPTS = {"input": "prompt", "rule": "title", "status": "status"}
"""``Console`` methods that render their first argument, by its keyword name."""

TABLE_CELLS = {"add_row": (), "add_column": ("header", "footer")}
"""``Table`` methods that render every positional argument, and the
keywords that are rendered too."""

PROMPT_CLASSES = frozenset({"Prompt", "Confirm", "IntPrompt", "FloatPrompt"})
"""Built or asked with a prompt, which they render: ``X(prompt)``, ``X.ask(prompt)``."""

ALLOWLIST: dict[tuple[str, str], str] = {}
"""``(path under the package, source of a printed argument)`` -> why it
holds no free text."""


def _constant(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return all(isinstance(v, ast.Constant) for v in node.values)
    return False


def _safe(node: ast.expr) -> bool:
    """A string constant, a ``Text``-building call, or a conditional
    expression both of whose branches are safe."""
    if _constant(node):
        return True
    if isinstance(node, ast.IfExp):
        return _safe(node.body) and _safe(node.orelse)
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in SAFE_LINES
    if isinstance(func, ast.Attribute):
        if func.attr == "assemble":
            return isinstance(func.value, ast.Name) and func.value.id == "Text"
        return func.attr in SAFE_LINES
    return False


def _keyword_is_false(call: ast.Call, name: str) -> bool:
    return any(
        k.arg == name and isinstance(k.value, ast.Constant) and k.value.value is False
        for k in call.keywords
    )


def _name(node: ast.expr) -> str | None:
    """The last name in ``node``: ``console`` for ``console`` and ``self.console``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _console_types(tree: ast.AST) -> set[str]:
    """``Console``, and each name ``tree`` imports it as."""
    out = {"Console"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "rich.console":
            out |= {a.asname for a in node.names if a.name == "Console" and a.asname}
    return out


def _consoles(tree: ast.AST) -> set[str]:
    """Names in ``tree`` bound to a ``Console(...)`` or annotated ``Console``."""
    types = _console_types(tree)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _name(node.value.func) in types:
                out |= {n for t in node.targets if (n := _name(t)) is not None}
        elif isinstance(node, ast.AnnAssign) and _name(node.annotation) in types:
            if (n := _name(node.target)) is not None:
                out.add(n)
        elif isinstance(node, ast.arg) and node.annotation and _name(node.annotation) in types:
            out.add(node.arg)
    return out


def _is_console(node: ast.expr, consoles: set[str]) -> bool:
    name = _name(node)
    return name is not None and ("console" in name.lower() or name in consoles)


def _print_method(node: ast.expr, consoles: set[str]) -> bool:
    """``node`` is ``x.print`` (not ``typer.print``) or a console's ``log``."""
    if not isinstance(node, ast.Attribute) or node.attr not in CONSOLE_PRINTS:
        return False
    if node.attr == "print":
        return _name(node.value) != "typer"
    return _is_console(node.value, consoles)


def _callee(node: ast.expr) -> str | None:
    """What a call is made through: ``say`` for ``say(...)``, ``self.say``
    for ``self.say(...)``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (receiver := _name(node.value)) is not None:
        return f"{receiver}.{node.attr}"
    return None


def _bindings(node: ast.AST) -> list[tuple[ast.expr, ast.expr]]:
    """``(target, value)`` for each name or attribute ``node`` binds: an
    assignment, one unpacking a tuple into a tuple, an annotated assignment
    with a value, or a walrus."""
    if isinstance(node, ast.Assign):
        pairs = []
        for target in node.targets:
            if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                pairs += list(zip(target.elts, node.value.elts, strict=False))
            else:
                pairs.append((target, node.value))
        return pairs
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [(node.target, node.value)]
    if isinstance(node, ast.NamedExpr):
        return [(node.target, node.value)]
    return []


def _print_names(tree: ast.AST, consoles: set[str]) -> set[str]:
    """Callees (see :func:`_callee`) that print with markup: Rich's ``print``
    imported with ``from rich import print``, and names or attributes bound
    to a console's ``print`` or ``log`` by any assignment (``say =
    console.print``, ``self.say: Callable = console.print``, ``a, say = x,
    console.print``, ``(say := console.print)``)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "rich":
            out |= {a.asname or a.name for a in node.names if a.name == "print"}
        for target, value in _bindings(node):
            if _print_method(value, consoles) and (callee := _callee(target)) is not None:
                out.add(callee)
    return out


def _first(call: ast.Call, keywords: tuple[str, ...]) -> list[ast.expr]:
    """The first positional argument and the ``keywords`` given."""
    return call.args[:1] + [k.value for k in call.keywords if k.arg in keywords]


def _rendered(call: ast.Call, consoles: set[str], print_names: set[str]) -> list[ast.expr] | None:
    """What ``call`` hands Rich to render as markup; ``None`` when it renders nothing."""
    func = call.func
    if _callee(func) in print_names:
        return call.args
    if isinstance(func, ast.Name):
        if func.id in PROMPT_CLASSES:
            return _first(call, ("prompt",))
        return None
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in PROMPT_CLASSES:
        return _first(call, ("prompt",))
    if _print_method(func, consoles):
        return call.args
    if func.attr in CONSOLE_PROMPTS and _is_console(func.value, consoles):
        return _first(call, (CONSOLE_PROMPTS[func.attr],))
    if func.attr in TABLE_CELLS:
        rendered = TABLE_CELLS[func.attr]
        return call.args + [k.value for k in call.keywords if k.arg in rendered]
    if func.attr == "ask" and _name(func.value) in PROMPT_CLASSES:
        return _first(call, ("prompt",))
    return None


def _renders(tree: ast.AST) -> list[tuple[ast.Call, list[ast.expr]]]:
    """Every call in ``tree`` that hands Rich a ``str`` to render as markup:
    a console's ``print``, ``log``, ``input``, ``rule`` and ``status``, a
    table's ``add_row`` and ``add_column``, a prompt built or asked, Rich's
    own ``print``, and a name bound to one of them; with what it renders."""
    consoles = _consoles(tree)
    print_names = _print_names(tree, consoles)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            rendered = _rendered(node, consoles, print_names)
            if rendered is not None:
                out.append((node, rendered))
    return out


def findings(source: str, path: str) -> list[str]:
    """Each argument a call in ``source`` hands to Rich as markup, as
    ``path:line: source of the argument``."""
    tree = ast.parse(source)
    out = []
    for call, rendered in sorted(_renders(tree), key=lambda r: r[0].lineno):
        if _keyword_is_false(call, "markup") and _keyword_is_false(call, "emoji"):
            continue
        for arg in rendered:
            if _safe(arg):
                continue
            text = ast.get_source_segment(source, arg) or ""
            if (path, text) in ALLOWLIST:
                continue
            out.append(f"{path}:{arg.lineno}: {text}")
    return out


def _package_findings() -> list[str]:
    found = []
    for file in sorted(PACKAGE.rglob("*.py")):
        rel = file.relative_to(PACKAGE).as_posix()
        found += findings(file.read_text(encoding="utf-8"), rel)
    return found


def test_no_print_hands_free_text_to_rich_as_markup() -> None:
    assert _package_findings() == []


def test_the_allowlist_is_exact() -> None:
    """An entry whose print is gone, or changed, must go too."""
    printed = set()
    for file in sorted(PACKAGE.rglob("*.py")):
        rel = file.relative_to(PACKAGE).as_posix()
        source = file.read_text(encoding="utf-8")
        for _, rendered in _renders(ast.parse(source)):
            printed |= {(rel, ast.get_source_segment(source, a) or "") for a in rendered}
    assert sorted(set(ALLOWLIST) - printed) == []


# ---------------------------------------------------------------------------
# The gate catches each way a site comes back
# ---------------------------------------------------------------------------


def test_the_gate_flags_free_text_in_markup() -> None:
    source = (
        "from rich.console import Console\n"
        "console = Console()\n"
        'console.print(f"[bold red]failed:[/] {exc}")\n'
        'console.print("[dim]" + message + "[/]")\n'
        "console.print(message)\n"
        'console.print("ok", detail)\n'
        'console.print(f"{task_id}: {reason}", markup=False)\n'
        'Confirm.ask(f"Remove {path}?")\n'
    )
    assert findings(source, "cli/x.py") == [
        'cli/x.py:3: f"[bold red]failed:[/] {exc}"',
        'cli/x.py:4: "[dim]" + message + "[/]"',
        "cli/x.py:5: message",
        "cli/x.py:6: detail",
        'cli/x.py:7: f"{task_id}: {reason}"',
        'cli/x.py:8: f"Remove {path}?"',
    ]


def test_the_gate_flags_every_other_renderer() -> None:
    source = (
        "from rich import print as rprint\n"
        "out = Console(stderr=True)\n"
        "def show(term: Console, table, msg, ok):\n"
        "    console.log(f'{msg}')\n"
        "    console.input(f'{msg}? ', password=True)\n"
        "    console.rule(title=msg)\n"
        "    with out.status(msg):\n"
        "        pass\n"
        "    term.log(msg)\n"
        "    table.add_row(msg, literal(msg))\n"
        "    table.add_column(header=msg)\n"
        "    IntPrompt.ask(msg)\n"
        "    FloatPrompt.ask(prompt=msg)\n"
        "    Prompt(msg).ask()\n"
        "    rich.prompt.Confirm(msg)\n"
        "    rprint(msg)\n"
        "    say = console.print\n"
        "    say(msg)\n"
        "    console.print(Text.from_markup(msg))\n"
        "    console.print(other.assemble(msg))\n"
        "    console.print(literal(msg) if ok else msg)\n"
    )
    assert findings(source, "cli/x.py") == [
        "cli/x.py:4: f'{msg}'",
        "cli/x.py:5: f'{msg}? '",
        "cli/x.py:6: msg",
        "cli/x.py:7: msg",
        "cli/x.py:9: msg",
        "cli/x.py:10: msg",
        "cli/x.py:11: msg",
        "cli/x.py:12: msg",
        "cli/x.py:13: msg",
        "cli/x.py:14: msg",
        "cli/x.py:15: msg",
        "cli/x.py:16: msg",
        "cli/x.py:18: msg",
        "cli/x.py:19: Text.from_markup(msg)",
        "cli/x.py:20: other.assemble(msg)",
        "cli/x.py:21: literal(msg) if ok else msg",
    ]
    assert findings("from rich import print\nprint(msg)\n", "cli/y.py") == ["cli/y.py:2: msg"]
    renamed = (
        "from rich.console import Console as C\n"
        "out = C()\n"
        "out.log(msg)\n"
        "class Shown:\n"
        "    def __init__(self, console):\n"
        "        self.say = console.print\n"
        "    def show(self, msg):\n"
        "        self.say(msg)\n"
    )
    assert findings(renamed, "cli/z.py") == ["cli/z.py:3: msg", "cli/z.py:8: msg"]
    bound = (
        "a, say = 1, console.print\n"
        "say(msg)\n"
        "if (shout := console.log):\n"
        "    shout(msg)\n"
        "class Shown:\n"
        "    def __init__(self, console):\n"
        "        self.say: object = console.print\n"
        "    def show(self, msg):\n"
        "        self.say(msg)\n"
    )
    assert findings(bound, "cli/w.py") == ["cli/w.py:2: msg", "cli/w.py:4: msg", "cli/w.py:9: msg"]


def test_the_gate_passes_constants_lines_and_plain_prints() -> None:
    source = (
        'console.print("[bold]Queue:[/]")\n'
        'console.print(f"[bold]no fields[/]")\n'
        'console.print(literal(styled("failed:", "bold red"), " ", exc))\n'
        'console.print(Text(f"{reason}", style="dim"))\n'
        'console.print(Text.assemble(("x", "bold"), reason))\n'
        'console.print(literal(reason) if ok else "[dim]none[/]")\n'
        "console.print(message, markup=False, emoji=False)\n"
        "console.print()\n"
        'table.add_row("[bold]id[/]", literal(task_id), styled(reason, "dim"))\n'
        'Confirm.ask("Apply?", default=False)\n'
        "logger.log(logging.INFO, message)\n"
        "job.status(message)\n"
        'typer.echo(f"{anything}")\n'
        'print(f"{anything}")\n'
    )
    assert findings(source, "cli/x.py") == []


def test_reintroducing_a_site_fails_the_package_gate() -> None:
    """The real package plus one reintroduced site: the gate names it."""
    source = (PACKAGE / "cli" / "queue_cmd.py").read_text(encoding="utf-8")
    broken = source + '\n\ndef _bad(console, exc):\n    console.print(f"[red]x:[/] {exc}")\n'
    found = findings(broken, "cli/queue_cmd.py")
    assert len(found) == 1
    assert found[0].endswith(': f"[red]x:[/] {exc}"')
