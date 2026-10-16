"""Every expansion of a ``config_dir`` goes through ``config.paths.expand_config_dir``.

``Path.expanduser`` raises ``RuntimeError`` for a ``~user`` that names no
user. A ``RuntimeError`` from the usage capture escaped the supervisor's
``safe_poll`` and ended its tick loop. ``expand_config_dir`` raises
``ConfigDirError`` instead, which each caller turns into its own error, so
this test fails on any ``.expanduser()`` call in ``src/`` outside
``config/paths.py`` that :data:`ALLOWED` does not list. A new call on a
``config_dir`` must use the helper; a new call on another kind of path
needs an entry here, with its reason.
"""

from __future__ import annotations

import ast
import textwrap
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
PACKAGE = REPO_ROOT / "src" / "claude_task_runner"
HELPER = Path("config/paths.py")

ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    ("runner/add_dirs.py", "_normalize"): (
        2,
        "a task's additional_dirs entry, not a config_dir",
    ),
    ("claude_init.py", "ensure_initialized"): (
        1,
        "trust_dir, the task's working_dir; config_dir goes through the helper",
    ),
    ("worktree/reclaim.py", "_task_entries"): (
        2,
        "a task's working_dir and deliverable paths, not a config_dir",
    ),
    ("worktree/reclaim.py", "_lock_path"): (
        1,
        "[worktree_reclaim].lock_file, not a config_dir",
    ),
}
"""``(path under the package, enclosing function)`` → (how many
``.expanduser()`` calls it may make, why they are not a ``config_dir``)."""


def expanduser_calls(source: str) -> Counter[str]:
    """How many ``.expanduser()`` calls each function in ``source`` makes.

    The key is the innermost enclosing function's name, ``<module>`` at the
    top level. ``os.path.expanduser(x)`` counts as well as
    ``Path(x).expanduser()``.
    """
    counts: Counter[str] = Counter()

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "expanduser"
            ):
                counts[scope] += 1
            visit(child, scope)

    visit(ast.parse(source), "<module>")
    return counts


def all_calls() -> dict[tuple[str, str], int]:
    """Every function in the package that calls ``.expanduser()``, outside the helper."""
    found: dict[tuple[str, str], int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if rel == HELPER:
            continue
        for scope, n in expanduser_calls(path.read_text(encoding="utf-8")).items():
            found[(rel.as_posix(), scope)] = n
    return found


def test_only_the_allowed_sites_call_expanduser() -> None:
    found = all_calls()
    allowed = {key: n for key, (n, _reason) in ALLOWED.items()}
    unexpected = {key: n for key, n in found.items() if allowed.get(key) != n}
    assert not unexpected, (
        "expanduser() outside config/paths.py, unexpected here: "
        f"{sorted(unexpected.items())}. Expand a config_dir with "
        "config.paths.expand_config_dir; for another kind of path, add the "
        "site to ALLOWED with its reason."
    )
    # A removed site leaves a stale entry; the allowlist must not outlive it.
    assert sorted(found) == sorted(allowed)


def test_the_helper_is_where_the_expansion_happens() -> None:
    assert expanduser_calls((PACKAGE / HELPER).read_text(encoding="utf-8")) == Counter(
        {"expand_config_dir": 1}
    )


class TestScanner:
    """The scanner itself: one that finds nothing would pass everything."""

    def test_finds_each_form_in_its_function(self) -> None:
        source = textwrap.dedent(
            """
            import os
            from pathlib import Path

            top = Path("~").expanduser()

            def one(config_dir):
                return Path(config_dir).expanduser()

            def two(a, b):
                def inner():
                    return os.path.expanduser(b)
                return Path(a).expanduser(), inner()
            """
        )
        assert expanduser_calls(source) == Counter({"<module>": 1, "one": 1, "two": 1, "inner": 1})

    def test_ignores_the_word_in_strings_and_comments(self) -> None:
        source = 'x = "Path.expanduser()"  # .expanduser() here is a comment\n'
        assert expanduser_calls(source) == Counter()
