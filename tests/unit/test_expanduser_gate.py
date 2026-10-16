"""Only ``config/paths.py`` expands a leading ``~`` in a path.

``Path.expanduser`` and ``os.path.expanduser`` raise ``RuntimeError`` for a
``~user`` that names no user. Code that must keep running, such as the
supervisor's tick loop, a dispatch thread or a reclaim pass, would end on
it. ``config/paths.py`` raises a typed error instead (``ConfigDirError`` for
an account's ``config_dir``, ``TaskPathError`` for a task path or another
path from the settings), which each caller turns into an error of its own
that fails or skips only the account or task concerned. So this test fails
on any reference to ``expanduser`` elsewhere in ``src/``. :data:`ALLOWED`
is empty; a site added there must say why its ``RuntimeError`` cannot
escape.
"""

from __future__ import annotations

import textwrap
from collections import Counter
from pathlib import Path

from ._reference_gate import references_by_scope

REPO_ROOT = Path(__file__).parent.parent.parent
PACKAGE = REPO_ROOT / "src" / "claude_task_runner"
HELPER = Path("config/paths.py")

ALLOWED: dict[tuple[str, str], tuple[int, str]] = {}
"""``(path under the package, qualified scope)`` → (how many references
to ``expanduser`` it may make, why a ``RuntimeError`` cannot escape)."""


def all_references() -> dict[tuple[str, str], int]:
    """Every scope in the package that refers to ``expanduser``, outside the helper."""
    found: dict[tuple[str, str], int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE)
        if rel == HELPER:
            continue
        source = path.read_text(encoding="utf-8")
        for scope, n in references_by_scope(source, "expanduser").items():
            found[(rel.as_posix(), scope)] = n
    return found


def test_only_the_helper_expands_a_path() -> None:
    found = all_references()
    allowed = {key: n for key, (n, _reason) in ALLOWED.items()}
    unexpected = {key: n for key, n in found.items() if allowed.get(key) != n}
    assert not unexpected, (
        "expanduser outside config/paths.py: "
        f"{sorted(unexpected.items())}. Use config.paths.expand_config_dir "
        "for a config_dir and config.paths.expand_task_path for any other "
        "path, and turn its error into the caller's own."
    )
    # A removed site leaves a stale entry; the allowlist must not outlive it.
    assert sorted(found) == sorted(allowed)


def test_the_helper_is_where_the_expansion_happens() -> None:
    source = (PACKAGE / HELPER).read_text(encoding="utf-8")
    assert references_by_scope(source, "expanduser") == Counter({"_expand": 1})


class TestScanner:
    """The scanner itself: one that finds nothing would pass everything."""

    def test_finds_each_form_in_its_scope(self) -> None:
        source = textwrap.dedent(
            """
            import os
            import os.path as osp
            from os.path import expanduser
            from os.path import expanduser as eu
            from pathlib import Path
            from pathlib import Path as P

            top = Path("~").expanduser()

            def one(config_dir):
                return Path(config_dir).expanduser()

            def two(a, b):
                def inner():
                    return os.path.expanduser(b)
                return Path.expanduser(Path(a)), inner()

            def aliases(a):
                return expanduser(a), eu(a), osp.expanduser(a), P(a).expanduser()

            def uncalled(a):
                expand = P.expanduser
                return list(map(eu, [a])), expand

            class Holder:
                def method(self, a):
                    return Path(a).expanduser()

                async def coroutine(self, a):
                    return os.path.expanduser(a)

                class Inner:
                    @staticmethod
                    def nested(a):
                        return Path(a).expanduser()

            async def coro(a):
                return Path(a).expanduser()
            """
        )
        assert references_by_scope(source, "expanduser") == Counter(
            {
                "<module>": 1,
                "one": 1,
                "two": 1,
                "two.inner": 1,
                "aliases": 4,
                "uncalled": 2,
                "Holder.method": 1,
                "Holder.coroutine": 1,
                "Holder.Inner.nested": 1,
                "coro": 1,
            }
        )

    def test_ignores_the_word_in_strings_comments_and_other_names(self) -> None:
        source = textwrap.dedent(
            """
            x = "Path.expanduser()"  # .expanduser() here is a comment
            expanduser_count = 0
            def expanduser_like(a):
                return a.expanduser_ish()
            """
        )
        assert references_by_scope(source, "expanduser") == Counter()
