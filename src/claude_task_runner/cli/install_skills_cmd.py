"""``claude-task-runner install-skills`` and ``uninstall-skills``.

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

import os
import shutil
from importlib import resources
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm

app = typer.Typer(no_args_is_help=False, invoke_without_command=False)


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


def _skills_target_dir() -> Path:
    """Resolve ``~/.claude/skills/`` and ensure it exists."""
    base = Path.home() / ".claude" / "skills"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _packaged_skill_dir(name: str) -> Path:
    """Resolve ``src/claude_task_runner/skills/<name>/`` on disk.

    Used both for symlink targets (where the package lives matters) and
    copy sources.
    """
    pkg = resources.files("claude_task_runner.skills") / name
    # ``files()`` returns a Traversable; coerce to a Path. For an
    # editable install this is the source tree; for a wheel install
    # it's the resolved .dist-info location.
    path = Path(str(pkg))
    if not path.exists():
        raise FileNotFoundError(f"packaged skill {name!r} not found at expected path {path}")
    return path


def _supports_symlinks(target_dir: Path) -> bool:
    """Probe whether we can create symlinks under ``target_dir``."""
    probe = target_dir / ".symlink_probe"
    try:
        probe.symlink_to(target_dir)
    except (OSError, NotImplementedError):
        return False
    finally:
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return True


def _install_one(
    name: str,
    *,
    target_dir: Path,
    use_symlinks: bool,
    overwrite: bool,
) -> tuple[bool, str]:
    """Install one skill. Returns ``(installed, detail)``.

    ``installed=False`` means we skipped (already present and
    overwrite=False).
    """
    src = _packaged_skill_dir(name)
    dst = target_dir / name

    if dst.exists() or dst.is_symlink():
        if not overwrite:
            return False, f"already present at {dst}"
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)

    if use_symlinks:
        dst.symlink_to(src)
        return True, f"symlinked → {src}"
    shutil.copytree(src, dst)
    return True, f"copied from {src}"


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
    target = _skills_target_dir()
    use_symlinks = (not copy) and _supports_symlinks(target)

    plan: list[tuple[str, Path]] = []
    for name in SKILL_NAMES:
        try:
            src = _packaged_skill_dir(name)
        except FileNotFoundError as exc:
            console.print(f"[bold red]missing skill:[/] {exc}")
            raise typer.Exit(code=2) from exc
        plan.append((name, src))

    console.print(
        f"[bold]Skills target:[/] {target}    [dim]mode: {'symlink' if use_symlinks else 'copy'}[/]"
    )
    for name, src in plan:
        dst = target / name
        existing = "[yellow](exists)[/]" if dst.exists() else ""
        console.print(f"  • {name}: [dim]{src}[/] {existing}")

    if not yes and not Confirm.ask("\nInstall these skills?", default=True):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    for name, _src in plan:
        try:
            installed, detail = _install_one(
                name,
                target_dir=target,
                use_symlinks=use_symlinks,
                overwrite=overwrite,
            )
        except OSError as exc:
            console.print(f"[bold red]{name}:[/] {exc}")
            continue
        marker = "[green]installed[/]" if installed else "[dim]skipped[/]"
        console.print(f"  {marker} {name}: {detail}")


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
    target = _skills_target_dir()

    present = [n for n in SKILL_NAMES if (target / n).exists() or (target / n).is_symlink()]
    if not present:
        console.print(f"[dim]No task-runner skills found under {target}[/]")
        return

    console.print(f"[bold]Will remove:[/] {target}")
    for name in present:
        path = target / name
        kind = "symlink" if path.is_symlink() else "directory"
        console.print(f"  • {name} ({kind})")

    if not yes and not Confirm.ask("\nRemove?", default=False):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    for name in present:
        path = target / name
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
        except OSError as exc:
            console.print(f"[red]failed to remove {name}: {exc}[/]")
            continue
        console.print(f"  [green]removed[/] {name}")


@app.command("list")
def list_installed() -> None:
    """Show which task-runner skills are present in ``~/.claude/skills/``."""
    console = Console()
    target = _skills_target_dir()
    for name in SKILL_NAMES:
        path = target / name
        if path.is_symlink():
            console.print(f"  [green]✓[/] {name}: symlinked → {os.readlink(path)}")
        elif path.exists():
            console.print(f"  [green]✓[/] {name}: copied at {path}")
        else:
            console.print(f"  [dim]✗[/] {name}: not installed")
