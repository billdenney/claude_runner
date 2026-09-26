"""``claude-task-runner install-skills`` — install, list and remove the skills.

* ``install-skills`` (no subcommand) — symlink or copy every packaged
  skill into ``~/.claude/skills/``.
* ``install-skills list``      — show which packaged skills are installed.
* ``install-skills uninstall`` — remove the packaged skills.

Skills are markdown files inside the package
(``src/claude_task_runner/skills/<name>/SKILL.md``). To activate them
in Claude Code we copy or symlink each skill directory into
``~/.claude/skills/<name>/``.

Symlinks are preferred when available (a ``pip install -e`` of the
package means edits flow through immediately). Operators on systems
without symlinks (or who prefer copies) get plain ``shutil.copy2``.

Like the cron / systemd installer, this asks for confirmation before
writing — managing user-global state is a category of action that
warrants a y/N prompt by default.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import assert_never

import typer
from rich.console import Console
from rich.prompt import Confirm

app = typer.Typer(no_args_is_help=False, invoke_without_command=False, rich_markup_mode=None)


OPERATOR_SKILL_NAMES = (
    "runner-status",
    "runner-usage",
    "runner-add-task",
    "runner-answer-sidecar",
    "runner-merge-claude-branches",
)
"""Operator-facing skills — invoked by the human running the queue
from an interactive ``claude`` session (status, usage, enqueue,
answer sidecars, consolidate branches)."""

AGENT_SKILL_NAMES = (
    "agent-stop-and-ask",
    "agent-bash-patterns",
)
"""Worker-facing skills — consulted by the *dispatched* agent, not the
operator. Dispatched workers run ``claude --print`` as the same Linux
user, so they discover skills from the same ``~/.claude/skills/`` the
operator skills land in (there is no per-worktree skill injection — a
worker's prompt is just ``task.prompt``). These must therefore be
installed alongside the operator skills for a worker to load them.
Each guards itself to no-op in interactive use (``agent-stop-and-ask``
defers to ``AskUserQuestion`` when ``$TASK_ID`` is unset;
``agent-bash-patterns`` is simply good universal advice)."""

SKILL_NAMES = OPERATOR_SKILL_NAMES + AGENT_SKILL_NAMES
"""All skills shipped with the package. Listed explicitly so we fail
loudly if a directory is missing rather than silently skipping.
``install-skills``, ``uninstall``, ``list``, and the doctor's
``skills_installed`` check all iterate this union."""


def skills_dir() -> Path:
    """``~/.claude/skills/``. Only an install creates it."""
    return Path.home() / ".claude" / "skills"


class SkillState(StrEnum):
    """What :func:`skill_state` finds at a skill's path in ``~/.claude/skills/``."""

    MISSING = "missing"
    """Nothing is there."""
    SYMLINKED = "symlinked"
    """A symlink to a directory with a ``SKILL.md``."""
    COPIED = "copied"
    """A directory with a ``SKILL.md``."""
    DANGLING = "dangling"
    """A symlink to nothing, as when its checkout was moved or deleted."""
    INCOMPLETE = "incomplete"
    """Something without a ``SKILL.md``, such as a copy that stopped partway."""


_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
"""The errors that mean nothing is at a path: no entry, a file where a
directory should be, or a symlink loop."""


def _stat_if_present(path: Path, *, follow_symlinks: bool = True) -> os.stat_result | None:
    """``path``'s stat, or None when nothing is there.

    Any other error, such as a permission error, is raised: the path
    could not be checked, which is not the same as absent. ``Path.exists()``
    cannot tell the two apart: Python 3.11 to 3.13 raise the permission
    error from it, and 3.14 returns False.
    """
    try:
        return path.stat(follow_symlinks=follow_symlinks)
    except OSError as exc:
        if exc.errno in _ABSENT_ERRNOS:
            return None
        raise


def skill_state(path: Path) -> SkillState:
    """Classify what is at ``path``, a skill's directory in ``~/.claude/skills/``.

    Raises OSError when ``path`` cannot be checked, for instance when a
    directory above it cannot be read.
    """
    entry = _stat_if_present(path, follow_symlinks=False)
    if entry is None:
        return SkillState.MISSING
    is_link = stat.S_ISLNK(entry.st_mode)
    if is_link and _stat_if_present(path) is None:
        return SkillState.DANGLING
    skill_md = _stat_if_present(path / "SKILL.md")
    if skill_md is None or not stat.S_ISREG(skill_md.st_mode):
        return SkillState.INCOMPLETE
    return SkillState.SYMLINKED if is_link else SkillState.COPIED


def _packaged_skill_dir(name: str) -> Path:
    """Resolve ``src/claude_task_runner/skills/<name>/`` on disk.

    Used both for symlink targets (where the package lives matters) and
    copy sources.
    """
    try:
        pkg = resources.files("claude_task_runner.skills") / name
    except ModuleNotFoundError as exc:
        # A wheel built without ``skills/`` has no package to import.
        # Raised as the missing-skill error the callers handle.
        raise FileNotFoundError(
            f"packaged skill {name!r} not found: "
            f"the claude_task_runner.skills package is missing ({exc})"
        ) from exc
    # ``files()`` returns a Traversable; coerce to a Path. For an
    # editable install this is the source tree; for a wheel install
    # it's the package's directory in site-packages.
    path = Path(str(pkg))
    if not path.exists():
        raise FileNotFoundError(f"packaged skill {name!r} not found at expected path {path}")
    return path


def _supports_symlinks(target_dir: Path) -> bool:
    """Probe whether we can create symlinks under ``target_dir``."""
    probe = target_dir / ".symlink_probe"
    try:
        probe.symlink_to(target_dir)
        supported = True
    except (OSError, NotImplementedError):
        supported = False
    # Not a ``finally`` block: a return there dropped any other error from
    # symlink_to, and Python 3.14 reports it at compile time (PEP 765).
    try:
        probe.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        supported = False
    return supported


def _install_one(
    name: str,
    *,
    target_dir: Path,
    use_symlinks: bool,
    overwrite: bool,
) -> tuple[bool, str]:
    """Install one skill. Returns ``(installed, detail)``.

    ``installed=False`` means we skipped (already present and
    overwrite=False). A symlink to nothing is replaced without
    ``overwrite``, since there is nothing in it to keep. Raises OSError
    when the install fails, including FileExistsError when something
    without a ``SKILL.md`` is in the way and ``overwrite`` is False.
    """
    src = _packaged_skill_dir(name)
    dst = target_dir / name

    replaced = ""
    state = skill_state(dst)
    if state is SkillState.DANGLING:
        replaced = f", replacing a broken symlink to {os.readlink(dst)}"
        dst.unlink()
    elif state is not SkillState.MISSING:
        if not overwrite:
            if state is SkillState.INCOMPLETE:
                raise FileExistsError(
                    f"{dst} has no SKILL.md; rerun with --overwrite to replace it"
                )
            return False, f"already present at {dst}"
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)

    if use_symlinks:
        dst.symlink_to(src)
        return True, f"symlinked → {src}{replaced}"
    try:
        shutil.copytree(src, dst)
    except FileExistsError:
        # Something else created dst after the check above; not ours to remove.
        raise
    except OSError as exc:
        _remove_partial_copy(dst, exc)
        raise
    return True, f"copied from {src}{replaced}"


def _remove_partial_copy(dst: Path, exc: OSError) -> None:
    """Remove what a failed copy left at ``dst``, so no later run takes it
    for an install. If that fails too, raise an OSError naming both errors."""
    try:
        shutil.rmtree(dst)
    except FileNotFoundError:
        pass  # The copy failed before it created dst.
    except OSError as cleanup_exc:
        raise OSError(
            f"{exc}; the partial copy at {dst} could not be removed: {cleanup_exc}"
        ) from exc


def _plan_note(dst: Path, *, overwrite: bool) -> str:
    """What the install will find at ``dst``, for the line that lists it."""
    try:
        state = skill_state(dst)
    except OSError as exc:
        return f" (cannot check: {exc})"
    if state is SkillState.MISSING:
        return ""
    if state is SkillState.SYMLINKED or state is SkillState.COPIED:
        return " (exists)"
    if state is SkillState.DANGLING:
        return " (broken symlink, will be replaced)"
    if state is SkillState.INCOMPLETE:
        return (
            " (no SKILL.md, will be replaced)" if overwrite else " (no SKILL.md, needs --overwrite)"
        )
    assert_never(state)  # pragma: no cover — mypy checks every state is handled above


@app.callback(invoke_without_command=True)
def install_skills(
    ctx: typer.Context,
    *,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/N confirmation."),
    copy: bool = typer.Option(False, "--copy", help="Copy files instead of symlinking."),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace existing skill directories of the same name.",
    ),
) -> None:
    """Install the task-runner skills into ``~/.claude/skills/``.

    Installs both the operator-facing skills (``runner-*``) and the
    worker-facing agent skills (``agent-*``) — dispatched workers read
    the same ``~/.claude/skills/`` as the operator, so the agent skills
    must be installed here for a worker to load them.

    Symlinks by default (so edits to the source tree are picked up
    automatically); use ``--copy`` to materialize independent copies.
    """
    if ctx.invoked_subcommand is not None:
        return

    console = Console()
    target = skills_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(
            f"cannot create {target}: {exc}",
            style="bold red",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        raise typer.Exit(code=2) from exc
    use_symlinks = (not copy) and _supports_symlinks(target)

    plan: list[tuple[str, Path]] = []
    for name in SKILL_NAMES:
        try:
            src = _packaged_skill_dir(name)
        except FileNotFoundError as exc:
            console.print(
                f"missing skill: {exc}",
                style="bold red",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            raise typer.Exit(code=2) from exc
        plan.append((name, src))

    console.print(
        f"[bold]Skills target:[/] {target}    [dim]mode: {'symlink' if use_symlinks else 'copy'}[/]"
    )
    for name, src in plan:
        console.print(
            f"  • {name}: {src}{_plan_note(target / name, overwrite=overwrite)}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    if not yes and not Confirm.ask("\nInstall these skills?", default=True):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    # A failed skill does not stop the rest, so every failure is
    # reported; the exit code then says the install is incomplete.
    failed = 0
    for name, _src in plan:
        try:
            installed, detail = _install_one(
                name,
                target_dir=target,
                use_symlinks=use_symlinks,
                overwrite=overwrite,
            )
        except OSError as exc:
            failed += 1
            console.print(
                f"  failed to install {name}: {exc}",
                style="bold red",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            continue
        console.print(
            f"  {'installed' if installed else 'skipped'} {name}: {detail}",
            style="green" if installed else "dim",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
    if failed:
        console.print(f"[bold red]Failed to install {failed} of {len(plan)} skills.[/]")
        raise typer.Exit(code=2)


_REMOVAL_KINDS = {
    SkillState.SYMLINKED: "symlink",
    SkillState.COPIED: "directory",
    SkillState.DANGLING: "broken symlink",
    SkillState.INCOMPLETE: "no SKILL.md",
}
"""How ``uninstall`` names each present state in the list it confirms."""


@app.command("uninstall")
def uninstall_skills(
    *,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/N confirmation."),
) -> None:
    """Remove the packaged skills from ``~/.claude/skills/``.

    Only removes the packaged names; never touches user skills
    that share a directory.
    """
    console = Console()
    target = skills_dir()

    present: list[tuple[str, SkillState]] = []
    unchecked = 0
    for name in SKILL_NAMES:
        try:
            state = skill_state(target / name)
        except OSError as exc:
            unchecked += 1
            console.print(
                f"  cannot check {name}: {exc}",
                style="bold red",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            continue
        if state is not SkillState.MISSING:
            present.append((name, state))
    if not present:
        if unchecked:
            console.print(f"[bold red]Could not check {unchecked} of {len(SKILL_NAMES)} skills.[/]")
            raise typer.Exit(code=2)
        console.print(
            f"No task-runner skills found under {target}",
            style="dim",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return

    console.print(f"[bold]Will remove:[/] {target}")
    for name, state in present:
        console.print(f"  • {name} ({_REMOVAL_KINDS[state]})")

    if not yes and not Confirm.ask("\nRemove?", default=False):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    failed = 0
    for name, _state in present:
        path = target / name
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError as exc:
            failed += 1
            console.print(
                f"  failed to remove {name}: {exc}",
                style="red",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            continue
        console.print(f"  [green]removed[/] {name}")
    if failed:
        console.print(f"[bold red]Failed to remove {failed} of {len(present)} skills.[/]")
    if unchecked:
        console.print(f"[bold red]Could not check {unchecked} of {len(SKILL_NAMES)} skills.[/]")
    if failed or unchecked:
        raise typer.Exit(code=2)


def _list_line(name: str, path: Path) -> tuple[str, str]:
    """``install-skills list``'s line for the skill at ``path``, and its style.

    Raises OSError when ``path`` cannot be checked.
    """
    state = skill_state(path)
    if state is SkillState.SYMLINKED:
        return f"  ✓ {name}: symlinked → {os.readlink(path)}", "green"
    if state is SkillState.COPIED:
        return f"  ✓ {name}: copied at {path}", "green"
    if state is SkillState.MISSING:
        return f"  ✗ {name}: not installed", "dim"
    if state is SkillState.DANGLING:
        return f"  ✗ {name}: broken symlink → {os.readlink(path)}, which does not exist", "red"
    if state is SkillState.INCOMPLETE:
        return f"  ✗ {name}: no SKILL.md in {path}", "red"
    assert_never(state)  # pragma: no cover — mypy checks every state is handled above


@app.command("list")
def list_installed() -> None:
    """Show which task-runner skills are in ``~/.claude/skills/``."""
    console = Console()
    target = skills_dir()
    unchecked = 0
    for name in SKILL_NAMES:
        try:
            line, style = _list_line(name, target / name)
        except OSError as exc:
            unchecked += 1
            line, style = f"  ? {name}: cannot check: {exc}", "bold red"
        console.print(line, style=style, markup=False, highlight=False, soft_wrap=True)
    if unchecked:
        console.print(f"[bold red]Could not check {unchecked} of {len(SKILL_NAMES)} skills.[/]")
        raise typer.Exit(code=2)
