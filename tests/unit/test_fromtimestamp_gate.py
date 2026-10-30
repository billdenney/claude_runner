"""Only ``clock.utc_from_timestamp`` turns a POSIX timestamp into a datetime.

``datetime.fromtimestamp`` raises ``ValueError``, ``OverflowError`` or
``OSError`` for a timestamp no datetime can hold, such as a file mtime past
year 9999, and ``OverflowError`` is not an ``OSError``. The helper turns all
three into one ``TimestampOutOfRange``. So this test fails on any reference
to ``fromtimestamp`` or ``utcfromtimestamp`` elsewhere in ``src/``.
:data:`ALLOWED` names a scope that cannot use the helper, how many
references it may make, and why its own ``except`` covers all three errors.
"""

from __future__ import annotations

import ast
import textwrap
from collections import Counter
from pathlib import Path

from ._reference_gate import references_by_scope

REPO_ROOT = Path(__file__).parent.parent.parent
PACKAGE = REPO_ROOT / "src" / "claude_task_runner"
HELPER = Path("clock.py")
NAMES = ("fromtimestamp", "utcfromtimestamp")
REPORT = Path("skills/runner-status/report.py")

ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    (REPORT.as_posix(), "gather"): (
        1,
        "runs under the system python3 and cannot import the package; the try "
        "around its conversion catches ValueError, OverflowError and OSError",
    ),
}
"""``(path under the package, qualified scope)`` → (how many references it
may make, why none of the three errors can escape)."""


def references(source: str) -> Counter[str]:
    """How often each scope in ``source`` refers to a conversion name."""
    counts: Counter[str] = Counter()
    for name in NAMES:
        counts += references_by_scope(source, name)
    return counts


def unexpected(
    sources: dict[Path, str],
) -> tuple[dict[tuple[str, str], int], list[tuple[str, str]]]:
    """For ``sources`` (path under the package → text), outside the helper:
    the references :data:`ALLOWED` does not allow, with their counts, and the
    allowed scopes that no longer make theirs."""
    found: dict[tuple[str, str], int] = {}
    for rel, source in sources.items():
        if rel == HELPER:
            continue
        for scope, n in references(source).items():
            found[(rel.as_posix(), scope)] = n
    allowed = {key: n for key, (n, _reason) in ALLOWED.items()}
    extra = {key: n for key, n in found.items() if allowed.get(key) != n}
    stale = sorted(set(allowed) - set(found))
    return extra, stale


def _package() -> dict[Path, str]:
    return {
        path.relative_to(PACKAGE): path.read_text(encoding="utf-8")
        for path in sorted(PACKAGE.rglob("*.py"))
    }


def test_only_the_helper_converts_a_timestamp() -> None:
    extra, stale = unexpected(_package())
    assert extra == {}, (
        f"fromtimestamp outside clock.utc_from_timestamp: {sorted(extra.items())}. "
        "Use clock.utc_from_timestamp and catch its TimestampOutOfRange."
    )
    assert stale == []


def test_the_helper_is_where_the_conversion_happens() -> None:
    source = (PACKAGE / HELPER).read_text(encoding="utf-8")
    assert references(source) == Counter({"utc_from_timestamp": 1})


def test_the_allowed_conversion_catches_all_three_errors() -> None:
    """The allowlisted call sits in a ``try`` whose handlers name every error
    ``fromtimestamp`` can raise."""
    tree = ast.parse((PACKAGE / REPORT).read_text(encoding="utf-8"))
    [gather] = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "gather"]
    guarded = [
        node
        for node in ast.walk(gather)
        if isinstance(node, ast.Try)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr in NAMES
            for statement in node.body
            for inner in ast.walk(statement)
        )
    ]
    assert len(guarded) == 1
    caught = {
        inner.id
        for handler in guarded[0].handlers
        if handler.type is not None
        for inner in ast.walk(handler.type)
        if isinstance(inner, ast.Name)
    }
    assert {"ValueError", "OverflowError", "OSError"} <= caught


# ---------------------------------------------------------------------------
# The gate catches each way a site comes back
# ---------------------------------------------------------------------------


def test_every_spelling_counts_in_its_scope() -> None:
    source = textwrap.dedent(
        """
        import datetime as dt
        from datetime import date, datetime
        from datetime import datetime as D

        def spellings(st, ts):
            a = datetime.fromtimestamp(st.st_mtime, tz=UTC)
            b = dt.datetime.fromtimestamp(ts)
            c = datetime.utcfromtimestamp(ts)
            d = date.fromtimestamp(ts)
            e = D.fromtimestamp(ts, UTC)
            convert = datetime.fromtimestamp
            return convert(ts)

        top = datetime.fromtimestamp(0, tz=UTC)

        class Holder:
            def method(self, ts):
                return datetime.fromtimestamp(ts, tz=UTC)
        """
    )
    assert references(source) == Counter({"spellings": 6, "<module>": 1, "Holder.method": 1})


def test_a_second_conversion_in_the_allowed_scope_fails() -> None:
    source = (PACKAGE / REPORT).read_text(encoding="utf-8")
    broken = source.replace(
        "def gather() -> Facts:\n",
        "def gather() -> Facts:\n    _x = datetime.fromtimestamp(0, UTC)\n",
        1,
    )
    assert broken != source
    extra, stale = unexpected({REPORT: broken})
    assert (extra, stale) == ({(REPORT.as_posix(), "gather"): 2}, [])


def test_a_method_named_like_the_allowed_scope_is_another_scope() -> None:
    extra, stale = unexpected(
        {REPORT: "class K:\n    def gather(self):\n        return datetime.fromtimestamp(0)\n"}
    )
    assert extra == {(REPORT.as_posix(), "K.gather"): 1}
    assert stale == [(REPORT.as_posix(), "gather")]


def test_reintroducing_a_bare_conversion_fails_the_package_gate() -> None:
    sources = _package()
    path = Path("supervisor/reconcile_silent.py")
    sources[path] += "\n\ndef _bad(st):\n    return datetime.fromtimestamp(st.st_mtime, tz=UTC)\n"
    extra, stale = unexpected(sources)
    assert (extra, stale) == ({(path.as_posix(), "_bad"): 1}, [])
