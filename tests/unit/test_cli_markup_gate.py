"""No print in the package hands free text to Rich as markup.

Rich reads markup (``[bold]``, ``[/]``) and emoji codes (``:b:``) in every
``str`` that ``Console.print`` (and ``Confirm.ask``) is given. A task id,
path, sidecar answer, account error or exception message holding either
would be restyled, cut short, turned into a symbol, or raise
``MarkupError``. So every value from outside the code reaches a print
inside a ``Text``, built with :func:`claude_task_runner.cli._helpers.literal`
(or ``Text`` itself), or the print turns both off (``markup=False,
emoji=False``).

This gate reads the source: each argument printed must be a string
constant, a ``literal(...)``/``Text(...)`` line, or printed with markup
and emoji off. :data:`ALLOWLIST` may name a printed expression proven to
hold no free text; it must stay exact. The tests at the bottom show the
gate fails on each way a site can be reintroduced.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "claude_task_runner"

SAFE_LINES = frozenset({"literal", "Text", "assemble"})
"""Calls whose result is a ``Text``, which Rich prints as it is."""

ALLOWLIST: dict[tuple[str, str], str] = {}
"""``(path under the package, source of a printed argument)`` -> why it
holds no free text."""


def _constant(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return all(isinstance(v, ast.Constant) for v in node.values)
    return False


def _keyword_is_false(call: ast.Call, name: str) -> bool:
    return any(
        k.arg == name and isinstance(k.value, ast.Constant) and k.value.value is False
        for k in call.keywords
    )


def _prints(tree: ast.AST) -> list[ast.Call]:
    """``x.print(...)`` calls other than ``typer``'s, and ``Confirm.ask`` /
    ``Prompt.ask``: everything that renders a ``str`` as markup."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        name = receiver.id if isinstance(receiver, ast.Name) else None
        is_print = node.func.attr == "print" and name != "typer"
        if is_print or (node.func.attr == "ask" and name in ("Confirm", "Prompt")):
            out.append(node)
    return out


def findings(source: str, path: str) -> list[str]:
    """Each argument a print in ``source`` hands to Rich as markup, as
    ``path:line: source of the argument``."""
    tree = ast.parse(source)
    out = []
    for call in sorted(_prints(tree), key=lambda c: c.lineno):
        if _keyword_is_false(call, "markup") and _keyword_is_false(call, "emoji"):
            continue
        for arg in call.args:
            if _constant(arg):
                continue
            if isinstance(arg, ast.Call):
                func = arg.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if called in SAFE_LINES:
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
        for call in _prints(ast.parse(source)):
            printed |= {(rel, ast.get_source_segment(source, a) or "") for a in call.args}
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


def test_the_gate_passes_constants_lines_and_plain_prints() -> None:
    source = (
        'console.print("[bold]Queue:[/]")\n'
        'console.print(f"[bold]no fields[/]")\n'
        'console.print(literal(("failed:", "bold red"), " ", exc))\n'
        'console.print(Text(f"{reason}", style="dim"))\n'
        "console.print(message, markup=False, emoji=False)\n"
        "console.print()\n"
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
