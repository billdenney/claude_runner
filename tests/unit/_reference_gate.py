"""Count where source code refers to a function, for the gates that confine it.

Shared by ``test_expanduser_gate`` (``expanduser`` only in
``config/paths.py``), ``test_cli_settings_gate`` (the CLI loads the queue
TOML only through ``load_settings_or_exit``) and ``test_fromtimestamp_gate``
(``fromtimestamp`` only in ``clock.utc_from_timestamp``). Each gate keeps
known-answer tests of the scanner: one that finds nothing would pass
everything.
"""

from __future__ import annotations

import ast
from collections import Counter

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def references_by_scope(source: str, name: str) -> Counter[str]:
    """How often each scope in ``source`` refers to the function ``name``.

    A reference is any attribute ``<expr>.name``, called or not, so
    ``Path(x).name()``, ``Path.name(p)``, ``module.name(x)`` and ``f =
    Path.name`` all count; and any use of a bare name that ``from ...
    import name`` or ``from ... import name as alias`` bound. The import
    itself does not count. The key is the qualified name of
    the innermost enclosing function or class (``Class.method``,
    ``outer.inner``), ``<module>`` at the top level.
    """
    tree = ast.parse(source)
    aliases = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == name
    }
    counts: Counter[str] = Counter()

    def visit(node: ast.AST, scope: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                visit(child, f"{scope}.{child.name}" if scope else child.name)
                continue
            if (isinstance(child, ast.Attribute) and child.attr == name) or (
                isinstance(child, ast.Name)
                and isinstance(child.ctx, ast.Load)
                and child.id in aliases
            ):
                counts[scope or "<module>"] += 1
            visit(child, scope)

    visit(tree, None)
    return counts
