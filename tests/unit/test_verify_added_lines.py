"""verify_added_lines.py: lines a merged branch added that the merge result lacks.

When two branches add lines at the same place, ``-X theirs`` keeps the later
merge's hunk and the earlier branch's lines vanish: a sentence appended to a
Notes line, or a whole canonical two branches added in different words. The
base-revert check guards only the base's content and the contribution verifier
reads filenames, headings and aliases, so neither sees it.

Each case builds the history for real and merges it the way merge_branches.sh
does, so the loss the check must find is the one ``-X theirs`` actually makes.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from ._git_world import git
from ._merge_skill_repo import (
    consolidate,
    load_helper,
    new_repo,
    push_task_branch,
    run_helper,
)

SCRIPT = "verify_added_lines.py"
REGISTER = "inst/references/covariate-columns.md"

BASE = """\
# Covariate columns

## Exposure

### CAV (**canonical for average concentration**)

- **Notes:** Base note.
- **Example models:** `Base_2000_x.R` (base).
"""
# Each branch appends a sentence to the Notes line and a model to the field.
BRANCH_A = BASE.replace("Base note.", "Base note. Alpha sentence.").replace(
    "(base).", "(base), `A_2020_a.R` (a note)."
)
BRANCH_B = BASE.replace("Base note.", "Base note. Beta sentence.").replace(
    "(base).", "(base), `B_2021_b.R` (b note)."
)
FOOTER = (
    "\n"
    "    A later merge's -X theirs hunk overwrote each, or a repair or hand edit\n"
    "    reworded it. Put each back, or union it into the line that replaced it.\n"
)
NOTES_A = "[claude/a] - **Notes:** Base note. Alpha sentence."
ENTRY_A = "[claude/a] Example models of Exposure / CAV: `A_2020_a.R` (a note)"

v: ModuleType = load_helper("verify_added_lines")
register_lines: ModuleType = load_helper("register_lines")


def check(repo: Path, *args: str | Path) -> tuple[int, str, str]:
    proc = run_helper(
        SCRIPT, "--repo", repo, "--branch", "consolidation", "--file", REGISTER, *args
    )
    return proc.returncode, proc.stdout, proc.stderr


def lost(path: str, *lines: str) -> str:
    listed = "".join(f"    {line}\n" for line in lines)
    return (
        f"\nERROR: (lines) {len(lines)} line(s) a merged branch added are missing from {path}:\n"
        f"{listed}{FOOTER}"
    )


def ok(path: str = REGISTER) -> str:
    return f"    (lines) OK — every line a merged branch added to {path} is in the merge result\n"


def commit_result(wt: Path, text: str, path: str = REGISTER) -> None:
    (wt / path).write_text(text)
    git(wt, "commit", "-qam", "Hand-union the register")


@pytest.fixture
def overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """claude/a and claude/b each extend the CAV Notes line and Example-models
    field; merged in that order, -X theirs keeps only claude/b's lines."""
    repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE})
    push_task_branch(repo, "claude/a", {REGISTER: BRANCH_A}, "Add A 2020 model")
    push_task_branch(repo, "claude/b", {REGISTER: BRANCH_B}, "Add B 2021 model")
    wt = consolidate(repo, ["origin/claude/a", "origin/claude/b"], "consolidation")
    assert (wt / REGISTER).read_text() == BRANCH_B, "the fixture did not reproduce the loss"
    return repo, wt


class TestReportsWhatALaterMergeOverwrote:
    def test_the_earlier_branchs_sentence_and_entry(self, overwritten: tuple[Path, Path]) -> None:
        """The base's own Notes line is gone too, but claude/a inherited it:
        that loss is the revert check's to report, not this one's."""
        repo, _ = overwritten
        assert check(repo) == (1, lost(REGISTER, NOTES_A, ENTRY_A), "")

    def test_the_union_puts_the_entry_back_and_the_sentence_stays_reported(
        self, overwritten: tuple[Path, Path]
    ) -> None:
        repo, _ = overwritten
        union = run_helper(
            "union_merge_lines.py", "--repo", repo, "--branch", "consolidation", "--file", REGISTER
        )
        assert union.returncode == 0, union.stderr
        assert check(repo) == (1, lost(REGISTER, NOTES_A), "")

    def test_a_hand_union_that_keeps_every_sentence_passes(
        self, overwritten: tuple[Path, Path]
    ) -> None:
        """Each branch's sentences are in one Notes line, with another
        branch's between them: both lines count as kept."""
        repo, wt = overwritten
        commit_result(
            wt,
            BASE.replace("Base note.", "Base note. Alpha sentence. Beta sentence.").replace(
                "(base).", "(base), `B_2021_b.R` (b note), `A_2020_a.R` (a note)."
            ),
        )
        assert check(repo) == (0, ok(), "")

    def test_a_hand_union_that_rewords_is_reported_for_each_branch(
        self, overwritten: tuple[Path, Path]
    ) -> None:
        """The decision whether the new wording keeps what each branch said
        is the operator's; merge_branches.sh --accept-line-losses records it."""
        repo, wt = overwritten
        commit_result(
            wt,
            BASE.replace("Base note.", "Base note; alpha and beta.").replace(
                "(base).", "(base), `B_2021_b.R` (b note), `A_2020_a.R` (a note)."
            ),
        )
        notes_b = "[claude/b] - **Notes:** Base note. Beta sentence."
        assert check(repo) == (1, lost(REGISTER, NOTES_A, notes_b), "")

    def test_a_sentence_kept_only_in_another_field_is_lost(
        self, overwritten: tuple[Path, Path]
    ) -> None:
        repo, wt = overwritten
        moved = BRANCH_B.replace(
            "- **Example models:**",
            "- **Description:** Base note. Alpha sentence.\n- **Example models:**",
        ).replace("(b note).", "(b note), `A_2020_a.R` (a note).")
        commit_result(wt, moved)
        assert check(repo) == (1, lost(REGISTER, NOTES_A), "")

    def test_a_canonical_two_branches_added_in_different_words(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both append ### VAF after CAV; -X theirs keeps the later copy, so
        the earlier one's heading and fields are lost under the same name."""
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE})
        a = BASE + "\n### VAF (**allele frequency, in per cent**)\n\n- **Units:** percent.\n"
        b = BASE + "\n### VAF (**baseline allele frequency**)\n\n- **Units:** % of reads.\n"
        push_task_branch(repo, "claude/a", {REGISTER: a}, "Register VAF (a)")
        push_task_branch(repo, "claude/b", {REGISTER: b}, "Register VAF (b)")
        consolidate(repo, ["origin/claude/a", "origin/claude/b"], "consolidation")
        assert check(repo) == (
            1,
            lost(
                REGISTER,
                "[claude/a] ### VAF (**allele frequency, in per cent**)",
                "[claude/a] - **Units:** percent.",
            ),
            "",
        )

    def test_a_branch_left_out_adds_nothing_to_check(self, overwritten: tuple[Path, Path]) -> None:
        repo, _ = overwritten
        left_out = BASE.replace("Base note.", "Base note. Gamma sentence.")
        push_task_branch(repo, "claude/c", {REGISTER: left_out}, "Add C (not merged)")
        git(repo, "fetch", "-q", "origin")
        code, stdout, stderr = check(repo)
        assert (code, stderr) == (1, "")
        assert stdout == (
            "    (lines) merge-set gate: skipped 1 branch(es) that consolidation did not merge"
            " (left out with --exclude-ref, or pushed after the survey): origin/claude/c\n"
            + lost(REGISTER, NOTES_A, ENTRY_A)
        )


class TestSetAside:
    """Wording a repair already wrote into the hand-union report is the hand
    union's to settle: counted, not reported again."""

    def test_a_line_the_report_holds_verbatim(
        self, overwritten: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, _ = overwritten
        report = tmp_path / "report.md"
        report.write_text(
            "In a dropped copy:\n\n````\n- **Notes:** Base note. Alpha sentence.\n````\n"
        )
        assert check(repo, "--set-aside", report) == (
            1,
            lost(REGISTER, ENTRY_A)
            + f"    (lines) 1 more from {REGISTER} a repair set aside in the hand-union report,"
            " for the hand union\n",
            "",
        )

    def test_an_entry_the_report_names_with_its_note(
        self, overwritten: tuple[Path, Path], tmp_path: Path
    ) -> None:
        repo, _ = overwritten
        report = tmp_path / "report.md"
        report.write_text(
            "````\n- **Notes:** Base note. Alpha sentence.\n````\n"
            "````\n`A_2020_a.R` (a note)\n````\n"
        )
        assert check(repo, "--set-aside", report) == (
            0,
            ok()
            + f"    (lines) 2 more from {REGISTER} a repair set aside in the hand-union report,"
            " for the hand union\n",
            "",
        )

    def test_a_filename_without_its_note_sets_nothing_aside(
        self, overwritten: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A verifier's line naming the model is not the entry's wording."""
        repo, _ = overwritten
        report = tmp_path / "report.md"
        report.write_text("    claude/a: `A_2020_a.R`\n")
        assert check(repo, "--set-aside", report) == (1, lost(REGISTER, NOTES_A, ENTRY_A), "")


def test_news_bullets_a_later_merge_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEWS.md has one append point, so every branch's bullet conflicts with
    every other's; after union_merge_news.py the check passes."""
    news = "# development version\n\n- Add Base 2000 x model.\n"
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": news})
    for name in ("a", "b"):
        bullet = f"- Add {name.upper()} 2020 {name} model."
        push_task_branch(
            repo, f"claude/{name}", {"NEWS.md": news.replace("\n\n", f"\n\n{bullet}\n", 1)}, name
        )
    consolidate(repo, ["origin/claude/a", "origin/claude/b"], "consolidation")
    args = ("--repo", repo, "--branch", "consolidation", "--file", "NEWS.md")
    before = run_helper(SCRIPT, *args)
    assert (before.returncode, before.stdout) == (
        1,
        lost("NEWS.md", "[claude/a] - Add A 2020 a model."),
    )
    union = run_helper("union_merge_news.py", "--repo", repo, "--branch", "consolidation")
    assert union.returncode == 0, union.stderr
    after = run_helper(SCRIPT, *args)
    assert (after.returncode, after.stdout, after.stderr) == (0, ok("NEWS.md"), "")


class TestTheSharedRule:
    """register_lines.py's Block, the rule the line check and the revert check
    share: a field line is kept word for word or with each of its sentences
    whole in one line of the same field; any other line only word for word."""

    BLOCK = (
        "### CAV_X (**x**)",
        "- **Notes:** Fixed-effect on clearance. Scaled to 70 kg; see below.",
        "- **Description:** Four.",
        "  - `ALKN` -- Thai 2023 Table 3.",
    )

    @pytest.mark.parametrize(
        ("line", "kept"),
        [
            ("- **Notes:** Fixed-effect on clearance. Scaled to 70 kg; see below.", True),
            ("- **Notes:**   Scaled to 70 kg;   see below.  ", True),
            ("- **Notes:** see below. Fixed-effect on clearance.", True),
            ("- **Notes:** Fixed.", False),
            ("- **Notes:** Scaled to 70 kg.", False),
            ("- **Description:** Fixed-effect on clearance.", False),
            ("### CAV_X (**x**)", True),
            ("### CAV", False),
            ("### CAV_X (**y**)", False),
            ("  - `ALKN` -- Thai 2023 Table 3.", True),
            ("  - `ALKN` -- Thai 2023", False),
            ("- `ALKN` -- Thai 2023 Table 3.", False),
        ],
    )
    def test_kept(self, line: str, kept: bool) -> None:
        assert register_lines.Block(self.BLOCK).holds(line) is kept


class TestBlocksAreKeptApart:
    RESULT = (
        "## S\n\n### CAV_X (**x**)\n\n- **Notes:** Scaled to 70 kg.\n\n"
        "### CAV (**c**)\n\n- **Notes:** Other.\n"
    )

    def test_a_sentence_under_another_canonical_is_not_kept(self) -> None:
        result = v.Result(self.RESULT, {})
        assert result.has_line("- **Notes:** Scaled to 70 kg.", ("S", "CAV (**c**)")) is False
        assert result.has_line("- **Notes:** Scaled to 70 kg.", ("S", "CAV_X (**x**)")) is True

    def test_a_heading_prefix_is_not_kept(self) -> None:
        result = v.Result(self.RESULT, {})
        assert result.has_line("### CAV", ("S", "CAV")) is False

    def test_a_canonical_moved_to_another_section_keeps_its_lines(self) -> None:
        result = v.Result(self.RESULT, {})
        assert result.has_line("- **Notes:** Other.", ("Elsewhere", "CAV (**c**)")) is True

    def test_a_line_lost_from_one_canonical_and_kept_by_another_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude/a extends CAV's Notes line; claude/b rewrites it, and gives
        VAF a Notes line word for word claude/a's. -X theirs keeps claude/b's
        CAV line, so claude/a's line is kept only under another canonical."""
        base = BASE + "\n### VAF (**v**)\n\n- **Notes:** V.\n"
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: base})
        a = base.replace("Base note.", "Base note. Shared sentence.")
        b = base.replace("Base note.", "Base note. B.").replace(
            "- **Notes:** V.", "- **Notes:** Base note. Shared sentence."
        )
        push_task_branch(repo, "claude/a", {REGISTER: a}, "a")
        push_task_branch(repo, "claude/b", {REGISTER: b}, "b")
        wt = consolidate(repo, ["origin/claude/a", "origin/claude/b"], "consolidation")
        assert "- **Notes:** Base note. B.\n" in (wt / REGISTER).read_text()
        assert check(repo) == (
            1,
            lost(REGISTER, "[claude/a] - **Notes:** Base note. Shared sentence."),
            "",
        )
