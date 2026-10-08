"""verify_no_base_reverts.py: register content the merge took back from the base.

``-X theirs`` resolves a conflicting hunk with the incoming branch's side, so a
branch cut from an older main can put back its older copy of a whole block.
On 2026-09-29 that dropped nine names from parameter-names.md's ``fm_<pathway>``
heading, with main's example entries and wording, and 16 models failed the
package's convention tests; and a branch's new ``CONMED_RTV_CC`` block cut
another block's notes line in two. No other step noticed either.

Each case builds the history for real and merges it the way merge_branches.sh
does, so the loss the check must find is the one ``-X theirs`` actually makes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._merge_skill_repo import (
    consolidate,
    load_helper,
    new_repo,
    push_task_branch,
    run_helper,
    update_main,
)

SCRIPT = "verify_no_base_reverts.py"
REGISTER = "inst/references/parameter-names.md"

FM_BLOCK = """\
### {names} (**canonical for a fraction metabolised**)

- **Example models:** {examples}.
- **Notes:** {notes}
"""
OTHER_BLOCK = """\
### ka (**canonical for an absorption rate**)

- **Example models:** `K_2019_k.R` (first order).
- **Notes:** Unrelated to the fm family.
"""


def register(names: str, examples: str, notes: str) -> str:
    fm = FM_BLOCK.format(names=names, examples=examples, notes=notes)
    return f"# Parameter names\n\n## Mechanistic parameters\n\n{fm}\n{OTHER_BLOCK}"


FORKED = register("fm_a", "`A_2020_a.R` (a)", "Old wording.")
# What a branch cut from FORKED makes of the block: one more name, one more model.
STALE = register("fm_a, fm_s", "`A_2020_a.R` (a), `S_2021_s.R` (s)", "Old wording.")
# What main makes of it meanwhile: a name, a model and new wording of its own.
MAIN = register("fm_a, fm_b", "`A_2020_a.R` (a), `B_2021_b.R` (b)", "New wording.")


def check(repo: Path, *files: str) -> tuple[int, str, str]:
    targets = [arg for path in files or (REGISTER,) for arg in ("--file", path)]
    proc = run_helper(SCRIPT, "--repo", repo, "--branch", "consolidation", *targets)
    return proc.returncode, proc.stdout, proc.stderr


def ok_line(path: str = REGISTER) -> str:
    return f"    (reverts) OK — nothing origin/main has in {path} was reverted by the merge\n"


FOOTER = (
    "\n"
    "    Usually -X theirs took an older copy of a block from a branch cut before main\n"
    "    changed it; a cut line is a new block pasted into the middle of an old one.\n"
    "    Put origin/main's content back by hand, keeping what the branches added. Nothing\n"
    "    was repaired automatically: which copy is right needs judgement.\n"
)


@pytest.fixture
def stale_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A branch cut before main changed the fm block, merged after it did."""
    repo = new_repo(tmp_path, monkeypatch, {REGISTER: FORKED})
    push_task_branch(repo, "claude/stale", {REGISTER: STALE}, "Add S 2021 and fm_s")
    update_main(repo, {REGISTER: MAIN}, "main adds fm_b, B 2021 and new wording")
    wt = consolidate(repo, ["origin/claude/stale"], "consolidation")
    assert (wt / REGISTER).read_text() == STALE, "the fixture did not reproduce the revert"
    return repo


def test_a_stale_branchs_copy_of_a_block_is_reported(stale_merge: Path) -> None:
    """Main's name, entry and wording are gone; the branch never touched them.

    The heading-name loss is the fm_<pathway> case: the heading kept the
    branch's own addition and lost main's.
    """
    assert check(stale_merge) == (
        1,
        "\n"
        f"ERROR: (reverts) 1 block(s) of {REGISTER} lost content that origin/main has:\n"
        "    ## Mechanistic parameters\n"
        "    ### fm_a, fm_b (**canonical for a fraction metabolised**)\n"
        "      missing heading name: fm_b\n"
        "      missing Example-models entry: `B_2021_b.R`\n"
        "      missing Example-models annotation of `B_2021_b.R`: (b)\n"
        "      missing line: - **Notes:** New wording.\n" + FOOTER,
        "",
    )


def test_restoring_the_base_content_clears_it(stale_merge: Path) -> None:
    """What an operator does about the report: main's block, plus what the branch added."""
    reconciled = register(
        "fm_a, fm_b, fm_s",
        "`A_2020_a.R` (a), `B_2021_b.R` (b), `S_2021_s.R` (s)",
        "New wording.",
    )
    (stale_merge / ".worktrees" / "consolidation" / REGISTER).write_text(reconciled)
    assert check(stale_merge) == (0, ok_line(), "")


def test_a_branchs_deliberate_edit_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main has a line the result lacks, but a merged branch's own diff removed it."""
    repo = new_repo(tmp_path, monkeypatch, {REGISTER: MAIN})
    edited = MAIN.replace("Unrelated to the fm family.", "Reworded on purpose.")
    push_task_branch(repo, "claude/edit", {REGISTER: edited}, "Reword the ka notes")
    wt = consolidate(repo, ["origin/claude/edit"], "consolidation")
    assert "Unrelated to the fm family." not in (wt / REGISTER).read_text()
    assert check(repo) == (0, ok_line(), "")


def test_a_block_main_added_that_a_stale_branch_dropped_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """restore_dropped_sections.py restores blocks branches add, never the base's.

    Main rewords the fm notes and adds a block right after them in one hunk;
    the branch rewords the same line its own way. -X theirs keeps the branch's
    side of that conflict, without main's wording or its new block.
    """
    repo = new_repo(tmp_path, monkeypatch, {REGISTER: FORKED})
    notes = "- **Notes:** Old wording.\n"
    push_task_branch(
        repo,
        "claude/stale",
        {REGISTER: FORKED.replace(notes, "- **Notes:** Old wording, extended by a branch.\n")},
        "Extend the fm notes",
    )
    added = "- **Notes:** New wording.\n\n### kout (**canonical for a loss rate**)\n\n- **Notes:** Added on main.\n"
    update_main(repo, {REGISTER: FORKED.replace(notes, added)}, "add kout")
    wt = consolidate(repo, ["origin/claude/stale"], "consolidation")
    assert "### kout" not in (wt / REGISTER).read_text(), "the fixture did not drop the block"
    assert check(repo) == (
        1,
        "\n"
        f"ERROR: (reverts) 2 block(s) of {REGISTER} lost content that origin/main has:\n"
        "    ## Mechanistic parameters\n"
        "    ### fm_a (**canonical for a fraction metabolised**)\n"
        "      missing line: - **Notes:** New wording.\n"
        "    ## Mechanistic parameters\n"
        "    ### kout (**canonical for a loss rate**)\n"
        "      the whole block is gone from this section of the merge result\n"
        "      missing heading name: kout\n"
        "      missing heading description: (**canonical for a loss rate**)\n"
        "      missing line: - **Notes:** Added on main.\n" + FOOTER,
        "",
    )


COMPARTMENTS = """\
# Compartment names

## Compartments

### col (**compartment holding colistin**)

- **Type:** compartment

## Metabolite suffixes

### col (**suffix for a colistin metabolite**)

- **Type:** suffix
"""


def test_a_token_two_sections_share_is_matched_within_its_own_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compartment-names.md registers some tokens both as a compartment and as a suffix.

    Matched by name alone, the suffix block would be compared with the
    compartment block, and its line reported missing.
    """
    path = "inst/references/compartment-names.md"
    repo = new_repo(tmp_path, monkeypatch, {path: COMPARTMENTS})
    extended = COMPARTMENTS.replace(
        "## Metabolite suffixes", "### depot (**dosing depot**)\n\n## Metabolite suffixes"
    )
    push_task_branch(repo, "claude/depot", {path: extended}, "Register depot")
    consolidate(repo, ["origin/claude/depot"], "consolidation")
    assert check(repo, path) == (0, ok_line(path), "")


CUT_BASE = """\
# Covariate columns

## Concomitant medication

### CONMED_RTV_AUC_12H (**canonical for ritonavir AUC over 0-12 h**)

- **Notes:** Specific scope. Sibling of `AUC_RTV`, the q24h once-daily form of the same exposure.
"""
# A branch's own commit adds a block by pasting it into the middle of that line.
CUT = """\
# Covariate columns

## Concomitant medication

### CONMED_RTV_AUC_12H (**canonical for ritonavir AUC over 0-12 h**)

- **Notes:** Specific scope.

### CONMED_RTV_CC (**canonical for ritonavir concentration**)

- **Notes:** A new member. Sibling of `AUC_RTV`, the q24h once-daily form of the same exposure.
"""


def test_a_line_a_pasted_block_cut_in_two_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reported even though the branch's own commit did it: that is never an edit."""
    path = "inst/references/covariate-columns.md"
    repo = new_repo(tmp_path, monkeypatch, {path: CUT_BASE})
    push_task_branch(repo, "claude/cc", {path: CUT}, "Register CONMED_RTV_CC")
    consolidate(repo, ["origin/claude/cc"], "consolidation")
    cut = (
        "- **Notes:** Specific scope. Sibling of `AUC_RTV`, the q24h once-daily form of the"
        " same exposure."
    )
    assert check(repo, path) == (
        1,
        "\n"
        f"ERROR: (reverts) 1 block(s) of {path} lost content that origin/main has:\n"
        "    ## Concomitant medication\n"
        "    ### CONMED_RTV_AUC_12H (**canonical for ritonavir AUC over 0-12 h**)\n"
        "      line cut in two, the rest now ending a line of ### CONMED_RTV_CC"
        f" (**canonical for ritonavir concentration**): {cut}\n" + FOOTER,
        "",
    )


def test_a_register_the_merge_deleted_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = new_repo(tmp_path, monkeypatch, {REGISTER: FORKED})
    push_task_branch(repo, "claude/x", {"inst/modeldb/X_2020_x.R": "# x\n"}, "Add X")
    wt = consolidate(repo, ["origin/claude/x"], "consolidation")
    (wt / REGISTER).unlink()
    assert check(repo) == (
        1,
        "\n"
        f"ERROR: (reverts) {REGISTER} is on origin/main but not in the merge result, and no"
        " merged branch deleted it\n",
        "",
    )


def test_a_register_no_one_has_is_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = new_repo(tmp_path, monkeypatch, {REGISTER: FORKED})
    push_task_branch(repo, "claude/x", {"inst/modeldb/X_2020_x.R": "# x\n"}, "Add X")
    consolidate(repo, ["origin/claude/x"], "consolidation")
    assert check(repo, "inst/references/none.md") == (
        0,
        "    (reverts) inst/references/none.md is not on origin/main; nothing to check.\n",
        "",
    )


class TestParsing:
    v = load_helper("verify_no_base_reverts")

    def test_a_multi_name_heading_is_one_name_per_entry(self) -> None:
        assert self.v.heading_names("fm_a, fm_b ,fm_c (**canonical (nested) x**)") == (
            frozenset({"fm_a", "fm_b", "fm_c"}),
            "(**canonical (nested) x**)",
        )

    def test_an_example_models_line_is_its_entries(self) -> None:
        """How many full stops end the line does not matter; prose after it does."""
        atoms = self.v.line_atoms("- **Example models:** `A.R` (a (b] c)..., `B.R`. See the Notes.")
        assert atoms == [
            ("example", "A.R"),
            ("annotation", "A.R", "(a (b] c)..."),
            ("example", "B.R"),
            ("after", "See the Notes"),
        ]
        assert self.v.line_atoms("- **Example models:** `A.R` (a).....") == [
            ("example", "A.R"),
            ("annotation", "A.R", "(a)"),
        ]

    def test_any_other_line_is_itself(self) -> None:
        assert self.v.line_atoms("- **Example models:**") == [("line", "- **Example models:**")]
        assert self.v.line_atoms("  - `A.R` (a sub-bullet)") == [
            ("line", "  - `A.R` (a sub-bullet)")
        ]
