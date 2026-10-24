"""CI's version pins agree with the dev extras, and every CI install uses them.

``.github/ci-constraints.txt`` pins mypy and types-PyYAML to exact versions
for CI's ``pip install -e '.[dev]' -c .github/ci-constraints.txt``. A pin
below a floor in ``pyproject.toml``'s dev extras would make that install
fail, and an install step without ``-c`` would take whatever was released
last; both are checked here, over every workflow's ``run`` steps, rather
than left to the README's instructions.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = REPO_ROOT / ".github" / "ci-constraints.txt"
CONSTRAINTS_ARG = ".github/ci-constraints.txt"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
STORE = REPO_ROOT / "src" / "claude_task_runner" / "queue" / "store.py"
PINNED = ("mypy", "types-pyyaml")

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


def _runs(node: object) -> list[str]:
    """Every ``run:`` script in a parsed workflow."""
    if isinstance(node, dict):
        found = [v for k, v in node.items() if k == "run" and isinstance(v, str)]
        return found + [r for v in node.values() for r in _runs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _runs(v)]
    return []


PIP = re.compile(r"pip(?:3(?:\.\d+)?)?")
"""``pip``, ``pip3`` or ``pip3.12``, as a command's basename."""


def _commands(script: str) -> list[list[str]]:
    """The commands of a ``run:`` script, each as words: backslash-continued
    lines joined, each line tokenised as a shell does (quotes kept together,
    comments dropped), then split at operator tokens (``&&``, ``||``,
    ``;``, ``|``, ``&``, parentheses). Splitting the text before tokenising
    broke a quoted ``|`` or ``;`` (``grep -E 'a|b'``), and shlex then raised."""
    out = []
    for line in re.sub(r"\\\n", " ", script).splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        words: list[str] = []
        for token in [*lexer, ";"]:
            if token and set(token) <= set("();|&"):
                if words:
                    out.append(words)
                words = []
            else:
                words.append(token)
    return out


def _package(word: str) -> str:
    """A requirement's distribution name, normalised: ``types_PyYAML>=6`` is
    ``types-pyyaml``."""
    return re.sub(r"[-_.]+", "-", re.split(r"[<>=!~;\[ ]", word, maxsplit=1)[0]).lower()


def _installs_pinned_tools(words: list[str]) -> bool:
    """``words`` is a pip install of the dev extras, mypy or types-PyYAML:
    ``pip``, ``pip3``, ``pip3.12`` or ``python -m pip``, with any options
    before ``install``."""
    at = next((i for i, w in enumerate(words) if PIP.fullmatch(w.rsplit("/", 1)[-1])), None)
    if at is None:
        return False
    rest = words[at + 1 :]
    while rest and rest[0].startswith("-"):
        rest = rest[1:]
    if not rest or rest[0] != "install":
        return False
    for word in rest[1:]:
        if re.search(r"\[[^]]*\bdev\b[^]]*\]", word) or _package(word) in PINNED:
            return True
    return False


def _is_constraints_file(path: str) -> bool:
    """The constraints file, from the checkout's root or by a longer path."""
    return path == CONSTRAINTS_ARG or path.endswith(f"/{CONSTRAINTS_ARG}")


def _constrained(words: list[str]) -> bool:
    for i, word in enumerate(words):
        following = words[i + 1] if i + 1 < len(words) else ""
        if word in ("-c", "--constraint") and _is_constraints_file(following):
            return True
        for prefix in ("-c", "--constraint="):
            if word.startswith(prefix) and _is_constraints_file(word[len(prefix) :]):
                return True
    return False


def _unconstrained(scripts: list[str]) -> tuple[int, list[str]]:
    """How many installs of the pinned tools ``scripts`` run, and those
    without the constraints file."""
    installs = [
        words for script in scripts for words in _commands(script) if _installs_pinned_tools(words)
    ]
    return len(installs), [shlex.join(w) for w in installs if not _constrained(w)]


def _unconstrained_installs(workflow: str) -> tuple[int, list[str]]:
    return _unconstrained(_runs(yaml.safe_load(workflow)))


def _shell_blocks(markdown: str) -> list[str]:
    """The fenced ``sh``/``bash``/``shell`` code blocks of a Markdown file."""
    return re.findall(r"^```(?:sh|bash|shell)\n(.*?)^```", markdown, re.MULTILINE | re.DOTALL)


def test_mypy_and_its_yaml_stubs_are_pinned() -> None:
    assert sorted(_pins()) == list(PINNED)


def test_every_pin_meets_the_dev_extras_floor() -> None:
    floors = _dev_floors()
    for name, pinned in _pins().items():
        assert name in floors, f"{name} has no floor in the dev extras"
        assert _version(pinned) >= _version(floors[name]), (name, pinned, floors[name])


def test_the_yaml_stub_floor_is_the_release_store_py_relies_on() -> None:
    """queue/store.py's two ignores are explained by the first types-PyYAML
    release that annotates the resolver hooks; every stub release its
    comment names must be the dev extras' floor."""
    named = set(re.findall(r"\b6\.0\.12\.\d{8}\b", STORE.read_text(encoding="utf-8")))
    assert named == {_dev_floors()["types-pyyaml"]}


def test_every_workflow_installs_the_pinned_tools_with_the_pins() -> None:
    workflows = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert workflows, f"no workflow in {WORKFLOWS}"
    total = 0
    for workflow in workflows:
        count, unconstrained = _unconstrained_installs(workflow.read_text(encoding="utf-8"))
        total += count
        assert unconstrained == [], workflow.name
    assert total > 0, "no workflow installs the dev extras"


def test_every_documented_install_uses_the_pins() -> None:
    """README.md and docs/ tell a developer to install the dev extras; the
    pins keep a local mypy run agreeing with CI's."""
    docs = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md"))]
    installs = {}
    for doc in docs:
        count, unconstrained = _unconstrained(_shell_blocks(doc.read_text(encoding="utf-8")))
        name = doc.relative_to(REPO_ROOT).as_posix()
        assert unconstrained == [], name
        if count:
            installs[name] = count
    assert installs == {"README.md": 2, "docs/first-time-setup.md": 1}


@pytest.mark.parametrize(
    ("run", "installs", "unconstrained"),
    [
        ("pip install -e '.[dev]' -c .github/ci-constraints.txt", 1, []),
        ("pip install -e '.[dev]' \\\n  -c .github/ci-constraints.txt", 1, []),
        ("pip install -e '.[dev]' \\\n  --upgrade", 1, ["pip install -e '.[dev]' --upgrade"]),
        (
            "python -m pip install --upgrade pip && pip install mypy==2.4.0",
            1,
            ["pip install mypy==2.4.0"],
        ),
        ("pip3 install --constraint=.github/ci-constraints.txt types-PyYAML", 1, []),
        ("pip install -e '.[dev,docs]'  # pinned below", 1, ["pip install -e '.[dev,docs]'"]),
        (
            "pip install -e . -c .github/other.txt mypy",
            1,
            ["pip install -e . -c .github/other.txt mypy"],
        ),
        ("python -m pip install --upgrade pip\npip install build", 0, []),
        ("pip -q install mypy", 1, ["pip -q install mypy"]),
        (
            "pip --disable-pip-version-check install -e '.[dev]' -c .github/ci-constraints.txt",
            1,
            [],
        ),
        ("python -m pip -q install types_PyYAML", 1, ["python -m pip -q install types_PyYAML"]),
        ("pip3.12 install mypy==2.4.0", 1, ["pip3.12 install mypy==2.4.0"]),
        ("(pip install mypy)", 1, ["pip install mypy"]),
        ("grep -E 'a|b' f && echo \"a; b\" | tee out", 0, []),
    ],
    ids=[
        "one-line",
        "continued",
        "continued-unpinned",
        "chained",
        "pip3",
        "comment",
        "other-file",
        "no-dev",
        "pip-option",
        "pip-long-option",
        "module-option-underscore",
        "pip3.12",
        "subshell",
        "quoted-operators",
    ],
)
def test_the_install_check_reads_whole_commands(
    run: str, installs: int, unconstrained: list[str]
) -> None:
    workflow = yaml.safe_dump({"jobs": {"x": {"steps": [{"run": run}]}}})
    assert _unconstrained_installs(workflow) == (installs, unconstrained)


def test_only_shell_blocks_are_read_from_markdown() -> None:
    markdown = (
        "Run `pip install -e '.[dev]'` inline.\n"
        "```sh\npip install -e '.[dev]'\n```\n"
        "```toml\ndev = ['mypy']\n```\n"
        "```bash\npip install mypy \\\n  -c .github/ci-constraints.txt\n```\n"
    )
    assert _unconstrained(_shell_blocks(markdown)) == (2, ["pip install -e '.[dev]'"])
