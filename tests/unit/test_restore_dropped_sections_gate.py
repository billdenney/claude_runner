"""The merge-set gate in the runner-merge-claude-branches restore script.

`restore_dropped_sections.py` repairs `### CANONICAL` blocks that `-X theirs`
drops during a large consolidation merge. Without a gate it also RESURRECTS
blocks that were removed on purpose, which is worse than the loss it repairs:
on an 80-branch nlmixr2lib round (2026-09-12) it proposed 31 blocks of which 20
were pre-rename spellings `main` had deliberately renamed away.

Three things must be skipped, and each has its own test below:

  (a) blocks from branches that are not ancestors of the consolidation branch
  (b) blocks a branch merely INHERITED from its own fork point (so `main`
      renamed them afterwards) rather than added
  (c) blocks removed by a repair commit on the consolidation branch itself
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src/claude_task_runner/skills/runner-merge-claude-branches/restore_dropped_sections.py"
)

REGISTER = "refs.md"


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    return out.stdout


def write_register(repo: Path, names: list[str]) -> None:
    body = "# Register\n\n## Section A\n\n"
    for n in names:
        body += f"### {n} (**canonical for {n}**)\n- **Type:** x\n\n"
    (repo / REGISTER).write_text(body, encoding="utf-8")


def commit(repo: Path, msg: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    write_register(r, ["KEEP_ME", "oldName"])
    commit(r, "base")
    # A task branch cut HERE inherits oldName without adding it.
    git(r, "branch", "task-inherits")
    # main then renames oldName -> NEW_NAME. A gate-less restore puts the old
    # spelling back, because it is absent from both main and the merge result.
    write_register(r, ["KEEP_ME", "NEW_NAME"])
    commit(r, "rename oldName -> NEW_NAME")
    git(r, "branch", "-f", "origin-main-marker")
    return r


def run(repo: Path, branch: str, base: str = "main") -> str:
    out = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo",
            str(repo),
            "--branch",
            branch,
            "--base",
            base,
            "--pattern",
            "origin/claude/*",
            "--file",
            REGISTER,
            "--check",
        ],
        capture_output=True,
        text=True,
    )
    return out.stdout + out.stderr


def make_remote_branch(repo: Path, name: str, from_ref: str, names: list[str], msg: str) -> None:
    """Create refs/remotes/origin/claude/<name> carrying `names`."""
    git(repo, "checkout", "-q", "-b", f"_tmp_{name}", from_ref)
    write_register(repo, names)
    commit(repo, msg)
    sha = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "update-ref", f"refs/remotes/origin/claude/{name}", sha)
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-q", "-D", f"_tmp_{name}")


def test_inherited_rename_is_not_resurrected(repo: Path) -> None:
    """(b) A branch cut before a rename still carries the old spelling."""
    make_remote_branch(
        repo, "inherits", "task-inherits", ["KEEP_ME", "oldName", "BRANCH_ADDED"], "task work"
    )
    git(repo, "checkout", "-q", "-b", "consolidation", "main")
    git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "--no-edit",
        "-X",
        "theirs",
        "refs/remotes/origin/claude/inherits",
    )
    # -X theirs took the branch's copy, so main's rename was rolled back here;
    # put the merged result back to main's spelling plus the branch's addition.
    write_register(repo, ["KEEP_ME", "NEW_NAME", "BRANCH_ADDED"])
    commit(repo, "reconcile")
    out = run(repo, "consolidation")
    assert "oldName" not in out, out
    assert "no dropped canonicals" in out, out


def test_genuine_loss_is_still_reported(repo: Path) -> None:
    """The gate must not silence a real merge loss.

    Modelled the way it actually happens: branch A adds a canonical; branch B
    is cut from a main that lacks it and rewrites the same region; merging B
    second with ``-X theirs`` takes B's copy and the block vanishes AT THE
    MERGE COMMIT. (Removing it in a later commit would be a deliberate
    retirement, which is a different case -- see the test below.)
    """
    make_remote_branch(repo, "adds", "main", ["KEEP_ME", "NEW_NAME", "REALLY_NEW"], "adds one")
    make_remote_branch(repo, "clobbers", "main", ["KEEP_ME", "NEW_NAME", "UNRELATED"], "other work")
    git(repo, "checkout", "-q", "-b", "consolidation", "main")
    git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "--no-edit",
        "-X",
        "theirs",
        "refs/remotes/origin/claude/adds",
    )
    git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "--no-edit",
        "-X",
        "theirs",
        "refs/remotes/origin/claude/clobbers",
    )
    assert "REALLY_NEW" not in (repo / REGISTER).read_text(), "fixture did not reproduce the loss"
    out = run(repo, "consolidation")
    assert "REALLY_NEW" in out, out


def test_non_ancestor_branch_is_skipped(repo: Path) -> None:
    """(a) A branch matching the pattern but never folded in contributes nothing."""
    make_remote_branch(repo, "notmerged", "main", ["KEEP_ME", "NEW_NAME", "OTHER_ROUND"], "other")
    git(repo, "checkout", "-q", "-b", "consolidation", "main")
    write_register(repo, ["KEEP_ME", "NEW_NAME"])
    commit(repo, "no merges at all")
    out = run(repo, "consolidation")
    assert "OTHER_ROUND" not in out, out
    assert "skipped 1" in out, out


def test_deliberate_rename_on_the_branch_is_skipped(repo: Path) -> None:
    """(c) A rename applied by a repair commit must not be undone on re-run."""
    make_remote_branch(repo, "ratio", "main", ["KEEP_ME", "NEW_NAME", "OLD_SPELLING"], "adds")
    git(repo, "checkout", "-q", "-b", "consolidation", "main")
    git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "--no-edit",
        "-X",
        "theirs",
        "refs/remotes/origin/claude/ratio",
    )
    # The merge kept OLD_SPELLING; a repair commit then renames it.
    write_register(repo, ["KEEP_ME", "NEW_NAME", "BETTER_SPELLING"])
    commit(repo, "apply operator naming ruling")
    out = run(repo, "consolidation")
    assert "OLD_SPELLING" not in out, out
    assert "deliberate rename/retire" in out, out
