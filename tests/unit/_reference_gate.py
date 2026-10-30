"""Count where source code refers to a function, for the gates that confine it.

Shared by ``test_expanduser_gate`` (``expanduser`` only in
``config/paths.py``), ``test_cli_settings_gate`` (the CLI loads the queue
TOML only through ``load_settings_or_exit``), ``test_fromtimestamp_gate``
(``fromtimestamp`` only in ``clock.utc_from_timestamp``) and
``test_parse_error_gate`` (every JSON, TOML and YAML parse catches
``ValueError``). Each gate keeps known-answer tests of the scanner: one that
finds nothing would pass everything.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Collection

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _does_nothing(handler: ast.ExceptHandler) -> bool:
    """Whether ``handler`` only re-raises (a bare ``raise``) or passes: the
    error then escapes, or vanishes without a word."""
    return all(
        isinstance(stmt, ast.Pass)
        or (isinstance(stmt, ast.Raise) and stmt.exc is None)
        or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        for stmt in handler.body
    )


def _catches(node: ast.Try | ast.TryStar, names: Collection[str]) -> bool:
    """Whether a handler of ``node`` that does something is a bare ``except``
    or names one of ``names``."""
    for handler in node.handlers:
        if _does_nothing(handler):
            continue
        if handler.type is None:
            return True
        types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        if any(isinstance(t, ast.Name) and t.id in names for t in types):
            return True
    return False


def references_by_scope(
    source: str,
    name: str,
    *,
    module: str | None = None,
    unless_caught: Collection[str] = (),
) -> Counter[str]:
    """How often each scope in ``source`` refers to the function ``name``.

    A reference is any attribute ``<expr>.name``, called or not, so
    ``Path(x).name()``, ``Path.name(p)``, ``module.name(x)`` and ``f =
    Path.name`` all count; and any use of a bare name that ``from ...
    import name`` or ``from ... import name as alias`` bound. The import
    itself does not count. The key is the qualified name of
    the innermost enclosing function or class (``Class.method``,
    ``outer.inner``), ``<module>`` at the top level.

    With ``module``, only references through that module count:
    ``module.name`` after ``import module``, ``alias.name`` after ``import
    module as alias``, and the bare names that ``from module import name``
    binds.

    With ``unless_caught`` (exception names), a call in the body of a
    ``try`` does not count when one of its handlers names one of them or is
    a bare ``except``, and does more than re-raise or pass. Only a call is
    covered: a reference passed on uncalled (``map(json.loads, ...)``, ``f =
    json.loads``) may run after the ``try``. So may anything in a function,
    class, lambda or generator expression defined in the body, each of which
    is checked on its own. The handlers, ``else`` and ``finally`` are not
    covered.
    """
    tree = ast.parse(source)
    receivers = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == module
    }
    aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (module is None or (node.module == module and node.level == 0))
        for alias in node.names
        if alias.name == name
    }
    counts: Counter[str] = Counter()

    def visit(node: ast.AST, scope: str | None, caught: bool) -> None:
        for field, value in ast.iter_fields(node):
            for child in value if isinstance(value, list) else [value]:
                if not isinstance(child, ast.AST):
                    continue
                if isinstance(child, _SCOPES):
                    visit(child, f"{scope}.{child.name}" if scope else child.name, False)
                    continue
                if isinstance(child, ast.Lambda | ast.GeneratorExp):
                    visit(child, scope, False)
                    continue
                covered = caught or (
                    bool(unless_caught)
                    and field == "body"
                    and isinstance(node, ast.Try | ast.TryStar)
                    and _catches(node, unless_caught)
                )
                called = isinstance(node, ast.Call) and field == "func"
                if not (covered and called) and (
                    (
                        isinstance(child, ast.Attribute)
                        and child.attr == name
                        and (module is None or ast.unparse(child.value) in receivers)
                    )
                    or (
                        isinstance(child, ast.Name)
                        and isinstance(child.ctx, ast.Load)
                        and child.id in aliases
                    )
                ):
                    counts[scope or "<module>"] += 1
                visit(child, scope, covered)

    visit(tree, None, False)
    return counts
