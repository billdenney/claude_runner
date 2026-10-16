"""NEWS.md union-merge: bullet detection, and which bullets a merged branch added.

1. bullets() matched only "- ". Real NEWS.md files mix markers -- nlmixr2lib's
   older entries use "* " and newer ones "- " -- so 327 of 378 base bullets
   were invisible and every "* "-style branch addition was silently skipped.
   Ketharanathan 2023 pentobarbital went missing while the coverage check
   reported NEWS complete, because the check shared the blind spot.

2. Which branch bullets to re-apply used to be decided by parsing "Add <Author>
   <Year>" and looking for a shipped model file with that author and year. On
   the 2026-09-29 consolidation that gate was wrong both ways: it kept bullets
   from branches the merge left out, when another model shared their author and
   year ("Add Wang 2020 caspofungin" rode in on a shipped Wang 2020 model), and
   it dropped bullets whose file stem spells the author or year differently:
   a lettered year (Chen_2021a_tacrolimus.R) or a surname particle ("Le
   Marouille", Marouille_2021_palbociclib.R). The gate is now provenance: a
   bullet is re-applied when a branch in the merge set added it in the commit
   that was merged.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from ._git_world import git
from ._merge_skill_repo import (
    consolidate,
    load_helper,
    new_repo,
    push_more,
    push_task_branch,
    run_helper,
    update_main,
)

u: ModuleType = load_helper("union_merge_news")


class TestBulletDetection:
    def test_dash_marker(self) -> None:
        assert u.bullets("- Add Smith 2020 drug (link) -- adults.") == [
            "- Add Smith 2020 drug (link) -- adults."
        ]

    def test_star_marker(self) -> None:
        """The regression: "* " bullets were invisible."""
        assert u.bullets("* Add Smith 2020 drug (link) -- adults.") == [
            "* Add Smith 2020 drug (link) -- adults."
        ]

    def test_mixed_markers_in_one_file(self) -> None:
        text = "# development version\n\n- Add A 2024 x.\n\n* Add B 2019 y.\n"
        assert u.bullets(text) == ["- Add A 2024 x.", "* Add B 2019 y."]

    def test_wrapped_continuation_lines_are_kept(self) -> None:
        text = "- Add Smith 2020 drug (link) --\n  adults with disease.\n"
        assert u.bullets(text) == ["- Add Smith 2020 drug (link) --\n  adults with disease."]

    def test_non_bullet_prose_is_ignored(self) -> None:
        assert u.bullets("# development version\n\nSome prose.\n") == []


class TestKeyNormalisesMarker:
    def test_same_entry_either_marker_is_one_key(self) -> None:
        """Otherwise a branch's "* " entry duplicates main's "- " entry."""
        assert u.key("- Add Smith 2020 drug.") == u.key("* Add Smith 2020 drug.")

    def test_whitespace_and_case_normalised(self) -> None:
        assert u.key("-  Add   Smith 2020 Drug.") == u.key("- add smith 2020 drug.")

    def test_different_entries_differ(self) -> None:
        assert u.key("- Add Smith 2020 drug.") != u.key("- Add Jones 2021 drug.")


BASE_NEWS = "# development version\n\n- Add Base 2000 x model.\n"
# name -> (bullet, model file). Each branch adds its bullet and its model.
BRANCHES = {
    # Shipped, and shares author and year with the left-out branch below.
    "wang": ("- Add Wang 2020 voriconazole model.", "Wang_2020_voriconazole.R"),
    # Left out of the merge, as the awaiting-sidecar branches were.
    "wang-left-out": ("- Add Wang 2020 caspofungin model.", "Wang_2020_caspofungin.R"),
    # A lettered year: the stem says 2021a, the bullet 2021.
    "chen": ("- Add Chen 2021 tacrolimus model.", "Chen_2021a_tacrolimus.R"),
    # A surname particle: the bullet says "Le Marouille", the stem Marouille.
    "le-marouille": (
        "* Add Le Marouille 2021 palbociclib model.",
        "Marouille_2021_palbociclib.R",
    ),
    # Merged, then pushed to again after the survey.
    "kim": ("- Add Kim 2022 x model.", "Kim_2022_x.R"),
}
MERGED = ["kim", "le-marouille", "chen", "wang"]
AFTER_THE_MERGE = "- Add Kim 2022 y model."


@pytest.fixture(scope="module")
def news_union(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str]]:
    """The stdout of one union_merge_news.py run over the scenario, and NEWS.md after it."""
    with pytest.MonkeyPatch.context() as mp:
        repo = new_repo(tmp_path_factory.mktemp("news"), mp, {"NEWS.md": BASE_NEWS})
        for name, (bullet, model) in BRANCHES.items():
            news = BASE_NEWS.replace("\n\n", f"\n\n{bullet}\n\n", 1)
            files = {"NEWS.md": news, f"inst/modeldb/{model}": "# model\n"}
            push_task_branch(repo, f"claude/{name}", files, f"Add {model}")
        wt = consolidate(repo, [f"origin/claude/{name}" for name in MERGED], "news")
        # -X theirs kept only the last branch's copy.
        assert (wt / "NEWS.md").read_text() == BASE_NEWS.replace(
            "\n\n", f"\n\n{BRANCHES['wang'][0]}\n\n", 1
        ), "the fixture did not reproduce the loss"
        kim_news = BASE_NEWS.replace(
            "\n\n", f"\n\n{AFTER_THE_MERGE}\n\n{BRANCHES['kim'][0]}\n\n", 1
        )
        push_more(repo, "claude/kim", {"NEWS.md": kim_news}, "Add Kim 2022 y")
        git(repo, "fetch", "-q", "origin")
        proc = run_helper("union_merge_news.py", "--repo", repo, "--branch", "news")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stderr == ""
        yield proc.stdout, (wt / "NEWS.md").read_text()


def test_news_union_is_base_plus_what_each_merged_branch_added(
    news_union: tuple[str, str],
) -> None:
    """The four cases below, at once.

    The merged branches' bullets go under the heading in ref-name order, above
    the base's history.
    """
    _, news = news_union
    assert news == (
        "# development version\n\n"
        "- Add Chen 2021 tacrolimus model.\n\n"
        "- Add Kim 2022 x model.\n\n"
        "* Add Le Marouille 2021 palbociclib model.\n\n"
        "- Add Wang 2020 voriconazole model.\n\n"
        "- Add Base 2000 x model.\n"
    )


def test_a_left_out_branch_sharing_author_and_year_adds_nothing(
    news_union: tuple[str, str],
) -> None:
    """The old gate kept it: a shipped Wang 2020 model matched its author and year."""
    stdout, news = news_union
    assert "Wang 2020 caspofungin" not in news
    assert "# merge-set gate: skipped 1 branch(es) that news did not merge" in stdout


def test_a_lettered_year_is_kept(news_union: tuple[str, str]) -> None:
    """The old gate looked for "chen 2021" and found only "chen 2021a"."""
    assert "- Add Chen 2021 tacrolimus model." in news_union[1]


def test_a_surname_particle_is_kept(news_union: tuple[str, str]) -> None:
    """The old gate compared "lemarouille" with the stem's "marouille"."""
    assert "* Add Le Marouille 2021 palbociclib model." in news_union[1]


def test_a_branch_that_moved_on_adds_only_what_was_merged(news_union: tuple[str, str]) -> None:
    """Its later bullet names a model this merge does not ship."""
    stdout, news = news_union
    assert "- Add Kim 2022 x model." in news
    assert AFTER_THE_MERGE not in news
    assert "# merge-set gate: origin/claude/kim moved on after news merged it;" in stdout


def test_an_inherited_bullet_main_reworded_is_not_brought_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch's copy of an older bullet is main's history, not the branch's news.

    The old union read each branch's whole file, so a bullet main reworded
    after the branch forked came back in its old wording beside the new one:
    three such duplicates reached the 2026-09-29 merge. The rebuild says what it
    drops.
    """
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": BASE_NEWS})
    push_task_branch(
        repo,
        "claude/stale",
        {"NEWS.md": BASE_NEWS.replace("\n\n", "\n\n- Add Stale 2019 s model.\n\n", 1)},
        "Add Stale 2019",
    )
    reworded = BASE_NEWS.replace("Base 2000 x model", "Base 2000 x model (reworded)")
    update_main(repo, {"NEWS.md": reworded}, "reword")
    wt = consolidate(repo, ["origin/claude/stale"], "news")
    assert "- Add Base 2000 x model.\n" in (wt / "NEWS.md").read_text()

    proc = run_helper("union_merge_news.py", "--repo", repo, "--branch", "news")
    assert (proc.returncode, proc.stderr) == (0, ""), proc.stdout
    assert (wt / "NEWS.md").read_text() == (
        "# development version\n\n- Add Stale 2019 s model.\n\n- Add Base 2000 x model (reworded).\n"
    )
    assert (
        "# dropping 1 bullet(s) that are neither on origin/main nor added by a merged branch:\n"
        "#   - Add Base 2000 x model.\n"
    ) in proc.stdout


REWORD_BASE = (
    "# development version\n\n"
    "- Add Base 2000 x model.\n\n"
    "- Add Old 1999 y model (doi:10.1000/wrong).\n\n"
    "- Add Gone 1998 z model.\n"
)


def _reword_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_too: bool = False
) -> tuple[Path, Path]:
    """A branch that corrects one inherited bullet and deletes another, and a
    branch that adds a model; optionally main rewords the corrected bullet too."""
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": REWORD_BASE})
    fixed = (
        REWORD_BASE.replace("doi:10.1000/wrong", "doi:10.1000/right")
        .replace("- Add Gone 1998 z model.\n", "")
        .rstrip("\n")
        + "\n"
    )
    push_task_branch(repo, "claude/fix", {"NEWS.md": fixed}, "Correct the Old 1999 DOI")
    # Inserted right above the bullet claude/fix corrects, so the two hunks
    # conflict and -X theirs brings the old wording back, as on 2026-10-10.
    added = REWORD_BASE.replace("- Add Old", "- Add New 2024 w model.\n\n- Add Old")
    push_task_branch(repo, "claude/new", {"NEWS.md": added}, "Add New 2024")
    if main_too:
        update_main(
            repo,
            {"NEWS.md": REWORD_BASE.replace("doi:10.1000/wrong", "doi:10.1000/mains")},
            "main rewords Old 1999",
        )
    wt = consolidate(repo, ["origin/claude/fix", "origin/claude/new"], "news")
    return repo, wt


def test_a_bullet_a_branch_reworded_is_rewritten_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading only additions kept both wordings: the base's old one in place
    and the branch's new one at the top (seven such duplicates on 2026-10-10).
    The branch's rewording now replaces the old bullet where it stands, and a
    bullet it deleted is gone."""
    repo, wt = _reword_world(tmp_path, monkeypatch)
    assert "doi:10.1000/wrong" in (wt / "NEWS.md").read_text(), "the fixture did not reproduce it"
    proc = run_helper("union_merge_news.py", "--repo", repo, "--branch", "news")
    assert (proc.returncode, proc.stderr) == (0, ""), proc.stdout
    assert (wt / "NEWS.md").read_text() == (
        "# development version\n\n"
        "- Add New 2024 w model.\n\n"
        "- Add Base 2000 x model.\n\n"
        "- Add Old 1999 y model (doi:10.1000/right).\n"
    )
    assert (
        "#   reworded by a merged branch: - Add Old 1999 y model (doi:10.1000/wrong)."
        in proc.stdout
    )
    assert "#   deleted by a merged branch: - Add Gone 1998 z model." in proc.stdout
    # Idempotent: a second run changes nothing.
    again = run_helper("union_merge_news.py", "--repo", repo, "--branch", "news", "--check")
    assert again.returncode == 0, again.stdout


def test_a_rewording_of_a_bullet_main_also_changed_is_added_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main rewrote the same bullet after the branch forked, so the base has
    neither wording the branch saw. The base's wording stays, the branch's is
    added at the top, and the run names it for review."""
    repo, wt = _reword_world(tmp_path, monkeypatch, main_too=True)
    proc = run_helper("union_merge_news.py", "--repo", repo, "--branch", "news")
    assert (proc.returncode, proc.stderr) == (0, ""), proc.stdout
    news = (wt / "NEWS.md").read_text()
    assert "- Add Old 1999 y model (doi:10.1000/mains)." in news
    assert news.index("- Add Old 1999 y model (doi:10.1000/right).") < news.index(
        "- Add Base 2000 x model."
    )
    assert (
        "# NOTE: origin/claude/fix rewords a bullet origin/main no longer has; its new wording"
        " is added at the top, check it against the base's own:"
        " - Add Old 1999 y model (doi:10.1000/right)."
    ) in proc.stdout


def _news(*items: str) -> str:
    return "# development version\n\n" + "\n\n".join(f"- {i}" for i in items) + "\n"


def _run_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base: str,
    first: tuple[str, str],
    second: tuple[str, str],
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Consolidate claude/<first> then claude/<second> and run the NEWS union."""
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": base})
    for name, text in (first, second):
        push_task_branch(repo, f"claude/{name}", {"NEWS.md": text}, f"Change {name}")
    wt = consolidate(repo, [f"origin/claude/{first[0]}", f"origin/claude/{second[0]}"], "news")
    proc = run_helper("union_merge_news.py", "--repo", repo, "--branch", "news")
    return proc, wt


class TestWhatABranchDidToInheritedBullets:
    """The rewording pass must tell a moved, reworded or deleted bullet apart."""

    def test_a_bullet_a_branch_only_moved_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reorder is not a deletion: the bullet is still in the branch's copy."""
        base = _news("A 2000 x.", "B 2001 y.", "C 2002 z.")
        proc, wt = _run_two(
            tmp_path,
            monkeypatch,
            base,
            ("mv", _news("C 2002 z.", "A 2000 x.", "B 2001 y.")),
            ("add", _news("N 2024 n.", "A 2000 x.", "B 2001 y.", "C 2002 z.")),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "- C 2002 z." in (wt / "NEWS.md").read_text()
        assert "deleted by a merged branch" not in proc.stdout

    def test_a_new_bullet_above_a_fix_does_not_take_the_fixed_bullets_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Insert N above X and fix X to X': X' replaces X, N goes to the top."""
        base = _news("X 2000 doi:wrong.", "Y 2001 y.", "Z 2002 z.")
        proc, wt = _run_two(
            tmp_path,
            monkeypatch,
            base,
            ("fix", _news("N 2024 n.", "X 2000 doi:right.", "Y 2001 y.", "Z 2002 z.")),
            ("add", _news("M 2023 m.", "X 2000 doi:wrong.", "Y 2001 y.", "Z 2002 z.")),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # New bullets go on top in ref-name order (claude/add, then claude/fix).
        assert (wt / "NEWS.md").read_text() == _news(
            "M 2023 m.", "N 2024 n.", "X 2000 doi:right.", "Y 2001 y.", "Z 2002 z."
        )
        assert "#   reworded by a merged branch: - X 2000 doi:wrong." in proc.stdout

    def test_two_middle_bullets_a_branch_deleted_go_with_their_blank_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = _news("A 2000 a.", "B 2001 b.", "C 2002 c.", "D 2003 d.")
        proc, wt = _run_two(
            tmp_path,
            monkeypatch,
            base,
            ("del", _news("A 2000 a.", "D 2003 d.")),
            ("add", _news("A 2000 a.", "N 2024 n.", "B 2001 b.", "C 2002 c.", "D 2003 d.")),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (wt / "NEWS.md").read_text() == _news("N 2024 n.", "A 2000 a.", "D 2003 d.")

    def test_two_branches_rewording_one_bullet_differently_keep_the_first_and_say_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = _news("X 2000 doi:wrong.", "Y 2001 y.")
        proc, wt = _run_two(
            tmp_path,
            monkeypatch,
            base,
            ("a", _news("X 2000 doi:right-a.", "Y 2001 y.")),
            ("b", _news("X 2000 doi:right-b.", "Y 2001 y.")),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        news = (wt / "NEWS.md").read_text()
        assert "doi:right-a" in news and "doi:wrong" not in news
        assert (
            "# NOTE: origin/claude/b also changes a bullet another merged branch changed"
            in proc.stdout
        )

    def test_a_branch_that_deletes_most_of_the_history_stops_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated NEWS.md on a branch is a mistake, not a dozen deletions."""
        many = [f"Old{k:02d} 19{k:02d} m." for k in range(15)]
        base = _news(*many)
        proc, _wt = _run_two(
            tmp_path,
            monkeypatch,
            base,
            ("trunc", _news(*many[:2])),
            ("add", _news("N 2024 n.", *many)),
        )
        assert proc.returncode == 2, proc.stdout
        assert "origin/claude/trunc deletes 13 of its 15 inherited bullets" in proc.stderr
        assert "# rebuilt" not in proc.stdout, "it must stop before rewriting NEWS.md"
