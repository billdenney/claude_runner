"""Every JSON, TOML and YAML parse in ``src/`` sits in a ``try`` that
catches ``ValueError``.

json, tomllib and PyYAML convert an integer literal with ``int()``, which
raises a bare ``ValueError`` for one longer than the interpreter's digit
limit (4300 digits by default), not their own decode error; ``json`` also
raises a bare ``UnicodeDecodeError`` for bytes that are not UTF-8. A
handler for ``JSONDecodeError`` or ``TOMLDecodeError`` alone lets both
through. So this test fails on a reference to a parse function that no
``try`` in its own function covers with a handler for ``ValueError``,
``Exception`` or ``BaseException``, or a bare ``except``. :data:`ALLOWED`
names a scope that may make such references, how many, and why.
"""

from __future__ import annotations

import textwrap
from collections import Counter
from pathlib import Path

import pytest

from ._reference_gate import references_by_scope

REPO_ROOT = Path(__file__).parent.parent.parent
PACKAGE = REPO_ROOT / "src" / "claude_task_runner"
LOADER = Path("config/loader.py")

PARSERS: dict[str, tuple[str, ...]] = {
    "json": ("load", "loads"),
    "tomllib": ("load", "loads"),
    "yaml": ("load", "load_all", "safe_load", "safe_load_all", "full_load"),
}
"""Each parsing module, and its functions that parse."""

GUARDS = frozenset({"ValueError", "Exception", "BaseException"})
"""Exception names whose handler catches the bare ``ValueError``."""

ALLOWED: dict[tuple[str, str], tuple[int, str]] = {
    (LOADER.as_posix(), "load_defaults"): (
        1,
        "parses the package's own settings.toml, which ships with the package and no user edits",
    ),
}
"""``(path under the package, qualified scope)`` → (how many references to
a parse function outside a ``ValueError`` handler it may make, why none
needs one). A parse whose caller catches the error on purpose belongs here,
with that caller named in the reason."""


def unguarded(source: str) -> Counter[str]:
    """How often each scope in ``source`` refers to a parse function outside
    a ``try`` that catches ``ValueError``."""
    counts: Counter[str] = Counter()
    for module, names in PARSERS.items():
        for name in names:
            counts += references_by_scope(source, name, module=module, unless_caught=GUARDS)
    return counts


def unexpected(
    sources: dict[Path, str],
) -> tuple[dict[tuple[str, str], int], list[tuple[str, str]]]:
    """For ``sources`` (path under the package → text): the unguarded
    references :data:`ALLOWED` does not allow, with their counts, and the
    allowed scopes that no longer make theirs."""
    found: dict[tuple[str, str], int] = {}
    for rel, source in sources.items():
        for scope, n in unguarded(source).items():
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


def test_every_parse_catches_value_error() -> None:
    extra, stale = unexpected(_package())
    assert extra == {}, (
        f"parses whose try does not catch ValueError: {sorted(extra.items())}. json, "
        "tomllib and PyYAML raise a bare ValueError for an integer past the digit "
        "limit; catch it where the decode error is caught."
    )
    assert stale == []


# ---------------------------------------------------------------------------
# Known answers for the scanner: one that finds nothing would pass everything
# ---------------------------------------------------------------------------


def test_a_handler_for_the_decode_error_alone_does_not_cover() -> None:
    source = textwrap.dedent(
        """
        import json
        import tomllib

        def read(path):
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                return None

        def read_toml(data):
            try:
                return tomllib.loads(data)
            except (tomllib.TOMLDecodeError, UnicodeDecodeError):
                return None
        """
    )
    assert unguarded(source) == Counter({"read": 1, "read_toml": 1})


@pytest.mark.parametrize(
    "handler",
    [
        "except ValueError:",
        "except (OSError, ValueError):",
        "except Exception:",
        "except BaseException:",
        "except:",
        "except* ValueError:",
    ],
)
def test_a_handler_that_catches_value_error_covers(handler: str) -> None:
    source = (
        "import json\n"
        "def read(fh):\n"
        "    try:\n"
        "        data = json.load(fh)\n"
        f"    {handler}\n"
        "        data = None\n"
        "    return data\n"
    )
    assert unguarded(source) == Counter()


def test_a_value_error_handler_on_an_enclosing_try_covers() -> None:
    """The inner ``try`` catches only the decode error; the outer one, in the
    same function, catches ``ValueError``."""
    source = textwrap.dedent(
        """
        import json

        def read(path):
            try:
                with path.open() as fh:
                    try:
                        return json.load(fh)
                    except json.JSONDecodeError:
                        return None
            except (OSError, ValueError):
                return None
        """
    )
    assert unguarded(source) == Counter()


def test_every_spelling_of_a_parse_counts_in_its_scope() -> None:
    source = textwrap.dedent(
        """
        import json as _json
        import yaml
        from json import load
        from tomllib import loads as parse_toml

        def spellings(fh, text):
            _json.loads(text)
            load(fh)
            parse_toml(text)
            yaml.safe_load(text)
            parse = _json.loads
            return parse

        top = yaml.load("a: 1", Loader=yaml.SafeLoader)

        class Holder:
            def method(self, text):
                return _json.loads(text)
        """
    )
    assert unguarded(source) == Counter({"spellings": 5, "<module>": 1, "Holder.method": 1})


def test_only_the_body_of_a_try_is_covered() -> None:
    """A parse in a handler, ``else`` or ``finally``, or in a function or
    lambda defined in the body, runs outside the ``try``."""
    source = textwrap.dedent(
        """
        import json

        def outer(text):
            try:
                def inner():
                    return json.loads(text)
                later = lambda: json.loads(text)
                now = json.loads(text)
            except ValueError:
                json.loads("{}")
            else:
                json.loads(text)
            finally:
                json.loads(text)
            return inner, later, now
        """
    )
    assert unguarded(source) == Counter({"outer.inner": 1, "outer": 4})


def test_a_load_from_another_module_is_not_a_parse() -> None:
    source = textwrap.dedent(
        """
        import pickle
        from claude_task_runner.supervisor import persistence as json
        from .tomllib import loads

        def read(fh, path, text):
            return pickle.load(fh), json.load(path), loads(text)
        """
    )
    assert unguarded(source) == Counter()


def test_a_second_parse_in_the_allowed_scope_fails() -> None:
    source = textwrap.dedent(
        """
        import tomllib

        def load_defaults(a, b):
            return tomllib.loads(a), tomllib.loads(b)
        """
    )
    extra, stale = unexpected({LOADER: source})
    assert (extra, stale) == ({(LOADER.as_posix(), "load_defaults"): 2}, [])


def test_a_method_named_like_the_allowed_scope_is_another_scope() -> None:
    source = textwrap.dedent(
        """
        import tomllib

        class K:
            def load_defaults(self, a):
                return tomllib.loads(a)
        """
    )
    extra, stale = unexpected({LOADER: source})
    assert extra == {(LOADER.as_posix(), "K.load_defaults"): 1}
    assert stale == [(LOADER.as_posix(), "load_defaults")]


def test_reintroducing_a_decode_error_only_handler_fails_the_package_gate() -> None:
    sources = _package()
    path = Path("supervisor/persistence.py")
    sources[path] += textwrap.dedent(
        """

        def _bad(path):
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                return None
        """
    )
    extra, stale = unexpected(sources)
    assert (extra, stale) == ({(path.as_posix(), "_bad"): 1}, [])
