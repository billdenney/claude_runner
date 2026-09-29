"""Every script the repository ships has a test module that runs it.

The skills' helper scripts ship in the wheel, and agents run them through
bash, python3 or Rscript; cron runs watchdog.sh; scripts/ holds maintainer
tools. The package imports none of them, so a script that no test runs can
break with nothing going red. Until 2026-09-26 nine of them had no test that
ran them. RUN_BY names a test module that runs each one, where its interpreter
is installed: CI has no R, so the R script runs only on machines that do. A
new script fails this gate until it has a test and an entry here.
"""

from __future__ import annotations

from pathlib import Path

UNIT_DIR = Path(__file__).parent
REPO_ROOT = UNIT_DIR.parents[1]
PACKAGE_DIR = REPO_ROOT / "src" / "claude_task_runner"
MERGE_SKILL = "src/claude_task_runner/skills/runner-merge-claude-branches"

RUN_BY: dict[str, str] = {
    "scripts/generate_synthetic_fixtures.py": "test_generate_synthetic_fixtures.py",
    "scripts/smoke_installed.py": "test_smoke_installed.py",
    "src/claude_task_runner/cron/watchdog.sh": "test_watchdog_cmd.py",
    "src/claude_task_runner/skills/runner-answer-sidecar/fetch_all.sh": "test_fetch_all_script.py",
    f"{MERGE_SKILL}/dedup_canonical_headers.py": "test_merge_skill_scripts.py",
    f"{MERGE_SKILL}/merge_branches.sh": "test_merge_skill_scripts.py",
    f"{MERGE_SKILL}/merge_set.py": "test_merge_skill_scripts.py",
    f"{MERGE_SKILL}/restore_dropped_sections.py": "test_restore_dropped_sections_gate.py",
    f"{MERGE_SKILL}/union_merge_lines.py": "test_merge_skill_scripts.py",
    f"{MERGE_SKILL}/union_merge_news.py": "test_union_merge_news.py",
    f"{MERGE_SKILL}/verify_branch_contributions.sh": "test_merge_skill_scripts.py",
    f"{MERGE_SKILL}/verify_register_placement.py": "test_merge_skill_scripts.py",
    f"{MERGE_SKILL}/verify_section_headers.py": "test_merge_skill_scripts.py",
    f"{MERGE_SKILL}/verify_vignettes_parallel.R": "test_merge_skill_scripts.py",
    "src/claude_task_runner/skills/runner-status/snapshot.sh": "test_snapshot_per_account.py",
}


def _shipped_scripts() -> set[str]:
    """Skill helpers, any shell or R script in the package, and scripts/."""
    found = {
        path
        for path in (PACKAGE_DIR / "skills").glob("*/*")
        if path.is_file() and path.name != "SKILL.md"
    }
    found |= {path for pattern in ("*.sh", "*.R") for path in PACKAGE_DIR.rglob(pattern)}
    found |= {path for path in (REPO_ROOT / "scripts").iterdir() if path.is_file()}
    return {path.relative_to(REPO_ROOT).as_posix() for path in found}


def test_every_script_has_a_test_module() -> None:
    assert sorted(_shipped_scripts()) == sorted(RUN_BY)


def test_each_test_module_names_its_script() -> None:
    """Catches an entry that points at the wrong module, or at a renamed one."""
    assert [
        script
        for script, module in RUN_BY.items()
        if Path(script).name not in (UNIT_DIR / module).read_text()
    ] == []
