"""CI's version pins agree with the dev extras, and every CI install uses them.

``.github/ci-constraints.txt`` pins mypy and types-PyYAML to exact versions
for CI's ``pip install -e '.[dev]' -c .github/ci-constraints.txt``. A pin
below a floor in ``pyproject.toml``'s dev extras would make that install
fail, and an install step without ``-c`` would take whatever was released
last; both are checked here rather than left to the README's instructions.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = REPO_ROOT / ".github" / "ci-constraints.txt"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_PIN = re.compile(r"^([A-Za-z0-9_.\-]+)==([0-9][0-9.]*)$")
_FLOOR = re.compile(r"^([A-Za-z0-9_.\-]+)>=([0-9][0-9.]*)$")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        assert match is not None, f"not an exact pin: {line!r}"
        pins[match.group(1).lower()] = match.group(2)
    return pins


def _dev_floors() -> dict[str, str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    floors: dict[str, str] = {}
    for requirement in project["project"]["optional-dependencies"]["dev"]:
        match = _FLOOR.match(requirement.replace(" ", ""))
        if match is not None:
            floors[match.group(1).lower()] = match.group(2)
    return floors


def test_mypy_and_its_yaml_stubs_are_pinned() -> None:
    assert sorted(_pins()) == ["mypy", "types-pyyaml"]


def test_every_pin_meets_the_dev_extras_floor() -> None:
    floors = _dev_floors()
    for name, pinned in _pins().items():
        assert name in floors, f"{name} has no floor in the dev extras"
        assert _version(pinned) >= _version(floors[name]), (name, pinned, floors[name])


def test_the_yaml_stub_floor_is_the_first_annotated_release() -> None:
    """queue/store.py's two ignores rely on it; see its comment."""
    assert _dev_floors()["types-pyyaml"] == "6.0.12.20260815"


def test_every_ci_install_of_the_dev_extras_uses_the_pins() -> None:
    installs = [
        line.strip()
        for line in CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
        if "pip install" in line and "[dev]" in line
    ]
    assert installs, "no dev-extras install found in ci.yml"
    assert all(line.endswith("-c .github/ci-constraints.txt") for line in installs), installs
