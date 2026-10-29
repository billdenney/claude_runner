"""No code in the package turns a POSIX timestamp into a datetime but one helper.

``datetime.fromtimestamp`` raises ``ValueError``, ``OverflowError`` or
``OSError`` for a timestamp no datetime can hold, such as a file mtime past
year 9999, and ``OverflowError`` is not an ``OSError``. Four of the six call
sites that took a non-constant timestamp caught too little, and two of those
crashed supervisor startup. :func:`claude_task_runner.clock.utc_from_timestamp`
turns all three into one ``TimestampOutOfRange``, so every conversion goes
through it.

This gate reads the source: any use of ``fromtimestamp`` or
``utcfromtimestamp``, called or not and on any receiver (``datetime``,
``date``, an alias), fails outside that helper, as does a bare name
``fromtimestamp``. :data:`ALLOWLIST` may name a function that cannot use the
helper and catches all three itself; it must stay exact. The tests at the
bottom show the gate fails on each way a site can come back.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "claude_task_runner"

NAMES = frozenset({"fromtimestamp", "utcfromtimestamp"})

HELPER = ("clock.py", "utc_from_timestamp")
"""``(path under the package, function)``: the one place a conversion lives."""

ALLOWLIST: dict[tuple[str, str], str] = {
    ("skills/runner-status/report.py", "gather"): (
        "runs under the system python3 and cannot import the package; its except "
        "catches ValueError, OverflowError and OSError around the conversion"
    ),
}
"""``(path under the package, enclosing function)`` -> why it may convert itself."""


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    """Each node in ``tree``, mapped to the name of the innermost function holding it."""
    out: dict[ast.AST, str] = {}

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            out[child] = name
            visit(child, name)

    visit(tree, "<module>")
    return out


def _uses(tree: ast.AST) -> list[tuple[ast.AST, str]]:
    """Each use of a conversion name in ``tree``, with its enclosing function."""
    functions = _enclosing_functions(tree)
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr in NAMES) or (
            isinstance(node, ast.Name) and node.id in NAMES
        ):
            found.append((node, functions.get(node, "<module>")))
    return sorted(found, key=lambda use: (use[0].lineno, use[0].col_offset))


def findings(source: str, path: str) -> list[str]:
    """Each use of ``fromtimestamp`` in ``source`` the gate refuses, as
    ``path:line: source of the use``."""
    out = []
    for node, function in _uses(ast.parse(source)):
        if (path, function) == HELPER or (path, function) in ALLOWLIST:
            continue
        out.append(f"{path}:{node.lineno}: {ast.get_source_segment(source, node)}")
    return out


def _package_sources() -> list[tuple[str, str]]:
    return [
        (file.relative_to(PACKAGE).as_posix(), file.read_text(encoding="utf-8"))
        for file in sorted(PACKAGE.rglob("*.py"))
    ]


def test_no_fromtimestamp_outside_the_helper() -> None:
    found = []
    for path, source in _package_sources():
        found += findings(source, path)
    assert found == []


def test_the_helper_and_the_allowlist_are_exact() -> None:
    """The helper still converts, and an allowlisted function whose
    conversion is gone, or moved, must go too."""
    using = set()
    for path, source in _package_sources():
        using |= {(path, function) for _, function in _uses(ast.parse(source))}
    assert HELPER in using
    assert sorted(set(ALLOWLIST) - using) == []


# ---------------------------------------------------------------------------
# The gate catches each way a site comes back
# ---------------------------------------------------------------------------


def test_the_gate_flags_every_spelling() -> None:
    source = (
        "import datetime as dt\n"
        "from datetime import date, datetime\n"
        "from datetime import datetime as D\n"
        "def f(st, ts):\n"
        "    a = datetime.fromtimestamp(st.st_mtime, tz=UTC)\n"
        "    b = dt.datetime.fromtimestamp(ts)\n"
        "    c = datetime.utcfromtimestamp(ts)\n"
        "    d = date.fromtimestamp(ts)\n"
        "    e = D.fromtimestamp(ts, UTC)\n"
        "    convert = datetime.fromtimestamp\n"
        "    return convert(ts)\n"
        "from datetime import datetime\n"
        "fromtimestamp = datetime.fromtimestamp\n"
    )
    assert findings(source, "x.py") == [
        "x.py:5: datetime.fromtimestamp",
        "x.py:6: dt.datetime.fromtimestamp",
        "x.py:7: datetime.utcfromtimestamp",
        "x.py:8: date.fromtimestamp",
        "x.py:9: D.fromtimestamp",
        "x.py:10: datetime.fromtimestamp",
        "x.py:13: fromtimestamp",
        "x.py:13: datetime.fromtimestamp",
    ]


def test_the_gate_passes_the_helper_and_its_callers() -> None:
    clock = (PACKAGE / "clock.py").read_text(encoding="utf-8")
    assert findings(clock, "clock.py") == []
    caller = (
        "from claude_task_runner.clock import utc_from_timestamp\n"
        "def g(st):\n"
        "    return utc_from_timestamp(st.st_mtime)\n"
    )
    assert findings(caller, "x.py") == []


def test_the_helper_exemption_is_by_function_not_file() -> None:
    """A second conversion in clock.py, outside the helper, is refused."""
    clock = (PACKAGE / "clock.py").read_text(encoding="utf-8")
    broken = clock + "\n\ndef _other(ts):\n    return datetime.fromtimestamp(ts, tz=UTC)\n"
    found = findings(broken, "clock.py")
    assert len(found) == 1
    assert found[0].endswith(": datetime.fromtimestamp")


def test_reintroducing_a_site_fails_the_package_gate() -> None:
    """The real reaper plus one reintroduced bare conversion: the gate names it."""
    path = "supervisor/reconcile_silent.py"
    source = (PACKAGE / path).read_text(encoding="utf-8")
    broken = source + "\n\ndef _bad(st):\n    return datetime.fromtimestamp(st.st_mtime, tz=UTC)\n"
    found = findings(broken, path)
    assert len(found) == 1
    assert found[0].endswith(": datetime.fromtimestamp")
