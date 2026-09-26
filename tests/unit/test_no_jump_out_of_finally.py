"""No ``return``, ``break`` or ``continue`` jumps out of a ``finally`` block.

Such a jump drops the exception the ``finally`` block is running for, on
every Python version. Python 3.14 also reports it at compile time
(PEP 765), as a SyntaxWarning that this project's
``filterwarnings = ["error"]`` turns into a SyntaxError, so there one such
jump stops every test module that imports its file from being collected.
``_supports_symlinks`` in ``cli/install_skills_cmd.py`` had one. CI runs
3.11 to 3.13, which do not report it (adding 3.14 to CI is planned as
H89), and ruff's B012, although it is on, does not look inside an
``except`` handler or an ``else`` branch within the ``finally``, which is
where that one was.

So this test walks the syntax tree itself. It flags any ``return``, and a
``break`` or ``continue`` that is not inside a loop that is itself inside
the ``finally``. Code in a function or class defined in the ``finally``
is its own scope and is not counted.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
SCANNED = ("src", "tests", "scripts")

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_LOOPS = (ast.For, ast.AsyncFor, ast.While)


def _jumps(body: list[ast.stmt], *, in_loop: bool) -> Iterator[ast.stmt]:
    """The statements in ``body``, a ``finally`` block or a block nested in
    one, that would jump out of that ``finally``."""
    for stmt in body:
        if isinstance(stmt, ast.Return) or (
            isinstance(stmt, ast.Break | ast.Continue) and not in_loop
        ):
            yield stmt
        elif isinstance(stmt, _LOOPS):
            # A break or continue in the loop's else clause belongs to an
            # enclosing loop, not this one.
            yield from _jumps(stmt.body, in_loop=True)
            yield from _jumps(stmt.orelse, in_loop=in_loop)
        elif not isinstance(stmt, _SCOPES):
            for field in ("body", "orelse", "finalbody"):
                yield from _jumps(getattr(stmt, field, []), in_loop=in_loop)
            for handler in getattr(stmt, "handlers", []):
                yield from _jumps(handler.body, in_loop=in_loop)
            for case in getattr(stmt, "cases", []):
                yield from _jumps(case.body, in_loop=in_loop)


def jumps_out_of_finally(tree: ast.AST) -> list[ast.stmt]:
    """Every statement in ``tree`` that jumps out of a ``finally`` block,
    once, even when it jumps out of several nested ones."""
    return list(
        dict.fromkeys(
            jump
            for node in ast.walk(tree)
            if isinstance(node, ast.Try | ast.TryStar)
            for jump in _jumps(node.finalbody, in_loop=False)
        )
    )


def report(paths: Iterable[Path], root: Path) -> list[str]:
    """``<path under root>:<line>: <return|break|continue>`` for each jump
    out of a ``finally`` block in ``paths``."""
    return [
        f"{path.relative_to(root)}:{jump.lineno}: {type(jump).__name__.lower()}"
        for path in paths
        for jump in jumps_out_of_finally(ast.parse(path.read_text(encoding="utf-8"), str(path)))
    ]


def test_no_file_jumps_out_of_a_finally_block() -> None:
    files = sorted(path for top in SCANNED for path in (REPO_ROOT / top).rglob("*.py"))
    # An empty or partial scan would pass whatever the code says.
    assert REPO_ROOT / "src/claude_task_runner/cli/install_skills_cmd.py" in files
    assert REPO_ROOT / "tests/unit/test_no_jump_out_of_finally.py" in files
    assert report(files, REPO_ROOT) == []


# Known answers. Each flagged case marks its offending line with
# "# jumps: <kind>", and the checker must report exactly that line.
# Python 3.14's compiler rejects every flagged case but "return in a
# loop": it stops looking for a return once inside a loop, though that
# return drops the exception all the same.
_FLAGGED = {
    "return": """
        def f():
            try:
                pass
            finally:
                return 1  # jumps: return
    """,
    "return in an if body": """
        def f(flag):
            try:
                pass
            finally:
                if flag:
                    return 1  # jumps: return
    """,
    "return in an except handler": """
        def f():
            try:
                pass
            finally:
                try:
                    pass
                except OSError:
                    return 1  # jumps: return
    """,
    "return in an else branch": """
        def f(flag):
            try:
                pass
            finally:
                if flag:
                    pass
                else:
                    return 1  # jumps: return
    """,
    "return in a loop": """
        def f():
            try:
                pass
            finally:
                for _ in range(3):
                    return 1  # jumps: return
    """,
    "return in a match case": """
        def f(x):
            try:
                pass
            finally:
                match x:
                    case 1:
                        return 1  # jumps: return
    """,
    "return in a with block": """
        def f(cm):
            try:
                pass
            finally:
                with cm:
                    return 1  # jumps: return
    """,
    "return in a nested finally": """
        def f():
            try:
                pass
            finally:
                try:
                    pass
                finally:
                    return 1  # jumps: return
    """,
    "break out of the enclosing loop": """
        def f():
            for _ in range(3):
                try:
                    pass
                finally:
                    break  # jumps: break
    """,
    "continue out of the enclosing loop": """
        def f():
            while True:
                try:
                    pass
                finally:
                    continue  # jumps: continue
    """,
    "break in a loop's else clause": """
        def f():
            for _ in range(3):
                try:
                    pass
                finally:
                    for _ in range(3):
                        pass
                    else:
                        break  # jumps: break
    """,
}

_ALLOWED = {
    "return after the try": """
        def f():
            try:
                pass
            finally:
                pass
            return 1
    """,
    "return in the try body": """
        def f():
            try:
                return 1
            finally:
                pass
    """,
    "return in a function defined in the finally": """
        def f():
            try:
                pass
            finally:
                def g():
                    return 1
    """,
    "return in a method of a class defined in the finally": """
        def f():
            try:
                pass
            finally:
                class C:
                    def g(self):
                        return 1
    """,
    "break in a loop inside the finally": """
        def f():
            try:
                pass
            finally:
                for _ in range(3):
                    break
    """,
    "continue in a loop inside the finally": """
        def f():
            try:
                pass
            finally:
                while True:
                    continue
    """,
}


def _case_file(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "case.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


@pytest.mark.parametrize("source", list(_FLAGGED.values()), ids=list(_FLAGGED))
def test_report_flags_a_jump_out_of_finally(tmp_path: Path, source: str) -> None:
    path = _case_file(tmp_path, source)
    [(line, kind)] = [
        (number, text.split("# jumps: ")[1])
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "# jumps: " in text
    ]
    assert report([path], tmp_path) == [f"case.py:{line}: {kind}"]


@pytest.mark.parametrize("source", list(_ALLOWED.values()), ids=list(_ALLOWED))
def test_report_allows_code_that_stays_in_finally(tmp_path: Path, source: str) -> None:
    assert report([_case_file(tmp_path, source)], tmp_path) == []
