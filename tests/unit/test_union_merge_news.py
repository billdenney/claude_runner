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
