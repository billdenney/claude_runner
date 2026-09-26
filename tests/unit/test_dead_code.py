"""Dead-code gate: nothing under ``src/`` may be unreachable or unused.

``supervisor/window.py`` sat in the package for months with no importer,
exercised only by its own tests, while live copies of its logic grew
elsewhere. The review that removed it found more of the same: helpers only
tests called, a supervisor state nothing entered, settings and task fields
nothing read. Written conventions did not stop any of it, so this module is
two mechanical checks.

* **Import reachability.** Every importable module under
  ``src/claude_task_runner`` must be in the static import closure of a
  ``[project.scripts]`` entry point. Imports inside functions count. This
  finds a whole dead module with no framework false positives.
* **vulture.** Every function, class, method, attribute and variable vulture
  reports as unused must go, or be listed in :data:`VULTURE_ALLOWLIST` with
  the reason it stays. Typer commands and callbacks and pydantic validators
  are ignored by decorator, ``model_config`` by name. Only ``src/`` is
  scanned: if tests counted as users, code that only its own tests call
  would pass.

Both allowlists must stay exact. An entry that no longer matches a finding
fails the gate, so removing dead code means deleting its entry too. The
known-answer tests at the bottom prove each check catches what it should.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from vulture import Vulture

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
PACKAGE = "claude_task_runner"

VULTURE_MIN_CONFIDENCE = 60
VULTURE_IGNORE_DECORATORS = [
    "@app.command",
    "@app.callback",
    "@field_validator",
    "@model_validator",
]
VULTURE_IGNORE_NAMES = ["model_config", "cls"]
"""``cls`` because vulture ignores an unused ``self`` but reports an unused
``cls`` (every validator's). It only matches names across the whole scan,
so without this the gate would pass only while some classmethod uses ``cls``."""
VULTURE_EXCLUDE = ["*/claude_task_runner/skills/*"]
"""The skills' helper scripts are standalone commands, not package code."""

UNREACHABLE_ALLOWLIST: dict[str, str] = {}
"""Modules allowed outside every entry point's import closure, with why."""

VULTURE_ALLOWLIST: dict[tuple[str, str], str] = {
    # --- Used, but not in a way vulture can see ------------------------------
    ("clock.py", "FakeClock"): "test double kept in src for tests to share (ADR-0009)",
    ("clock.py", "advance"): "FakeClock method; tests call it",
    ("clock.py", "set_to"): "FakeClock method; tests call it",
    ("usage/source.py", "FakeUsageSource"): "test double kept in src for tests to share",
    ("usage/source.py", "set_readings"): "FakeUsageSource method; tests call it",
    ("observability.py", "_reset_for_tests"): "tests reset logging setup with it",
    ("queue/schema.py", "resumed_from_session"): "RunRecord field, persisted to state YAML",
    ("queue/schema.py", "killed_by_cap"): "RunRecord field, persisted to state YAML",
    ("queue/schema.py", "responded_at"): "SidecarResponse field, persisted to response JSON",
    ("usage/capture.py", "logfile_read"): "set on the pexpect child, which reads it",
    ("cli/usage_cmd.py", "EXIT_OK"): "names exit code 0 beside the other EXIT_* codes",
    ("cron/backoff.py", "next_check_at"): (
        "WatchdogDecision field; its docstring says `watchdog tick` does not read it"
    ),
    # --- Dead; each names what removes it or decides its fate --------------
    ("runner/retry.py", "classify"): "removed by chore/retire-failure-classifier-patterns",
    ("runner/retry.py", "should_auto_resume"): (
        "removed by chore/retire-failure-classifier-patterns"
    ),
    ("runner/stream.py", "StreamWarning"): "removed by fix/surface-skipped-stream-lines",
    ("runner/stream.py", "skipped_lines"): "read once fix/surface-skipped-stream-lines lands",
    ("throttle/decision.py", "target_concurrency"): (
        "ADR-0022's slowdown ramp, which dispatch ignores; its card decides wire or remove"
    ),
    ("runner/stream.py", "text_excerpt"): "unread stream-parse output",
    ("runner/stream.py", "usage_delta"): "unread stream-parse output",
    ("runner/stream.py", "subtype"): "unread stream-parse output",
    ("runner/stream.py", "event_count"): "unread stream-parse output",
}
"""vulture findings that may stay, keyed by (path under the package, name)."""


# --- import reachability ---------------------------------------------------


def _package_modules(src_root: Path, package: str) -> dict[str, Path]:
    """Dotted name -> file for every importable module of ``package``.

    A path with a component that is not an identifier (the skills'
    hyphenated script directories) cannot be imported, so it is skipped.
    """
    modules: dict[str, Path] = {}
    for path in sorted((src_root / package).rglob("*.py")):
        parts = list(path.relative_to(src_root).with_suffix("").parts)
        if not all(part.isidentifier() for part in parts):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules[".".join(parts)] = path
    return modules


def _imported_names(module: str, path: Path) -> set[str]:
    """Every dotted name ``module`` imports, at any depth, relative ones resolved.

    ``from pkg import name`` yields both ``pkg`` and ``pkg.name``, because
    ``name`` may be a submodule. Callers keep only names that are modules.
    """
    is_package = path.name == "__init__.py"
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = module.split(".") if is_package else module.split(".")[:-1]
                base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join([*base_parts, node.module] if node.module else base_parts)
            else:
                base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def import_closure(src_root: Path, package: str, roots: list[str]) -> set[str]:
    """Modules of ``package`` reachable by importing each of ``roots``.

    Importing ``a.b.c`` also runs ``a`` and ``a.b``, so every parent
    package of an import counts as reached too.
    """
    modules = _package_modules(src_root, package)
    edges: dict[str, set[str]] = {}
    for module, path in modules.items():
        reached: set[str] = set()
        for name in _imported_names(module, path):
            parts = name.split(".")
            reached.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        edges[module] = reached & modules.keys()
    seen: set[str] = set()
    stack = [root for root in roots if root in modules]
    while stack:
        module = stack.pop()
        if module not in seen:
            seen.add(module)
            stack.extend(edges[module])
    return seen


def console_script_modules(pyproject: Path) -> list[str]:
    """The module half of each ``[project.scripts]`` ``module:function`` entry."""
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]
    return sorted({target.split(":", 1)[0] for target in scripts.values()})


# --- vulture --------------------------------------------------------------


def vulture_findings(package_dir: Path) -> set[tuple[str, str]]:
    """``(path under package_dir, name)`` for every unused item vulture reports."""
    scanner = Vulture(
        verbose=False,
        ignore_names=VULTURE_IGNORE_NAMES,
        ignore_decorators=VULTURE_IGNORE_DECORATORS,
    )
    scanner.scavenge([str(package_dir)], exclude=VULTURE_EXCLUDE)
    return {
        (Path(item.filename).resolve().relative_to(package_dir.resolve()).as_posix(), item.name)
        for item in scanner.get_unused_code(min_confidence=VULTURE_MIN_CONFIDENCE)
    }


# --- the gate ----------------------------------------------------------------


class TestDeadCodeGate:
    def test_entry_points_are_the_console_scripts(self) -> None:
        assert console_script_modules(REPO_ROOT / "pyproject.toml") == [f"{PACKAGE}.cli"]

    def test_every_module_is_reachable_from_an_entry_point(self) -> None:
        roots = console_script_modules(REPO_ROOT / "pyproject.toml")
        modules = _package_modules(SRC_ROOT, PACKAGE)
        unreachable = sorted(set(modules) - import_closure(SRC_ROOT, PACKAGE, roots))
        unexplained = [m for m in unreachable if m not in UNREACHABLE_ALLOWLIST]
        assert not unexplained, (
            "no entry point imports these modules, even inside a function. Delete "
            "them, or add each to UNREACHABLE_ALLOWLIST with the reason it stays:\n"
            + "\n".join(f"  {m}  ({modules[m].relative_to(REPO_ROOT)})" for m in unexplained)
        )
        stale = sorted(set(UNREACHABLE_ALLOWLIST) - set(unreachable))
        assert not stale, f"UNREACHABLE_ALLOWLIST entries that are reachable or gone: {stale}"

    def test_no_unused_code_outside_the_allowlist(self) -> None:
        findings = vulture_findings(SRC_ROOT / PACKAGE)
        unexplained = sorted(findings - VULTURE_ALLOWLIST.keys())
        assert not unexplained, (
            "vulture reports these as unused. Delete them, or add each to "
            "VULTURE_ALLOWLIST with the reason it stays:\n"
            + "\n".join(f"  src/{PACKAGE}/{path}: {name}" for path, name in unexplained)
        )
        stale = sorted(VULTURE_ALLOWLIST.keys() - findings)
        assert not stale, (
            "VULTURE_ALLOWLIST entries vulture no longer reports; delete them:\n"
            + "\n".join(f"  {path}: {name}" for path, name in stale)
        )


# --- known answers: the instruments catch what they should ------------------


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class TestImportClosureKnownAnswer:
    def test_finds_exactly_the_unreachable_modules(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            {
                "pkg/__init__.py": "",
                "pkg/cli.py": "from pkg import a\nfrom . import rel\n",
                "pkg/a.py": "def f():\n    from pkg.sub import deep\n",
                "pkg/rel.py": "",
                "pkg/sub/__init__.py": "",
                "pkg/sub/deep.py": "from ..sub import sibling\n",
                "pkg/sub/sibling.py": "",
                "pkg/orphan.py": "import pkg.a\n",
                "pkg/sub/orphan2.py": "",
                "pkg/not-a-module/script.py": "import pkg.orphan\n",
            },
        )
        reached = import_closure(tmp_path, "pkg", ["pkg.cli"])
        assert reached == {
            "pkg",
            "pkg.cli",
            "pkg.a",
            "pkg.rel",
            "pkg.sub",
            "pkg.sub.deep",
            "pkg.sub.sibling",
        }
        assert set(_package_modules(tmp_path, "pkg")) - reached == {
            "pkg.orphan",
            "pkg.sub.orphan2",
        }

    def test_console_scripts_are_read_from_pyproject(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "x"\n[project.scripts]\n'
            'a = "pkg.cli:main"\nb = "pkg.tools.run:go"\nc = "pkg.cli:other"\n',
            encoding="utf-8",
        )
        assert console_script_modules(pyproject) == ["pkg.cli", "pkg.tools.run"]


class TestVultureKnownAnswer:
    def test_reports_dead_code_and_ignores_framework_hooks(self, tmp_path: Path) -> None:
        package = tmp_path / "claude_task_runner"
        _write(
            package,
            {
                "mod.py": (
                    "from pydantic import BaseModel, field_validator\n"
                    "import typer\n"
                    "app = typer.Typer()\n\n"
                    "def used():\n    return 1\n\n"
                    "def dead_function():\n    return used()\n\n"
                    "@app.command()\ndef cli_command():\n    pass\n\n"
                    "@app.callback()\ndef cli_callback():\n    pass\n\n"
                    "class Model(BaseModel):\n"
                    "    model_config = {}\n"
                    "    value: int = 0\n\n"
                    "    @field_validator('value')\n"
                    "    @classmethod\n"
                    "    def _check(cls, v):\n        return v\n\n"
                    "print(Model().value)\n"
                ),
                "skills/tool-x/script.py": "def dead_in_script():\n    pass\n",
            },
        )
        assert vulture_findings(package) == {("mod.py", "dead_function")}
