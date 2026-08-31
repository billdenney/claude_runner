"""NEWS.md union-merge: bullet detection and the shipped-model gate.

Both regressions covered here shipped because nothing in the test suite
touched the merge-skill scripts.

1. bullets() matched only "- ". Real NEWS.md files mix markers -- nlmixr2lib's
   older entries use "* " and newer ones "- " -- so 327 of 378 base bullets
   were invisible and every "* "-style branch addition was silently skipped.
   Ketharanathan 2023 pentobarbital went missing while the coverage check
   reported NEWS complete, because the check shared the blind spot.

2. bullet_ships() decides whether a bullet names a model the merge actually
   ships, by parsing "Add <Author> <Year>". On the real bullet "Add 14
   published imatinib population PK models transcribed from the Yang 2025
   external evaluation" the non-greedy match swallowed the whole phrase as the
   author, matched no shipped model, and DROPPED the one entry covering all 14
   imatinib models.
"""

import importlib.util
from pathlib import Path
from typing import ClassVar

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "src/claude_task_runner/skills/runner-merge-claude-branches/union_merge_news.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("union_merge_news", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


u = _load()


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
        assert len(u.bullets(text)) == 2

    def test_wrapped_continuation_lines_are_kept(self) -> None:
        text = "- Add Smith 2020 drug (link) --\n  adults with disease.\n"
        got = u.bullets(text)
        assert len(got) == 1
        assert "adults with disease" in got[0]

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


class TestBulletShips:
    TOKENS: ClassVar[set[str]] = {"smith 2020", "vandenberg 2025"}

    def test_shipped_author_year_kept(self) -> None:
        assert u.bullet_ships("- Add Smith 2020 drug (link).", self.TOKENS)

    def test_unshipped_author_year_dropped(self) -> None:
        assert not u.bullet_ships("- Add Jones 2021 other (link).", self.TOKENS)

    def test_compound_surname_kept(self) -> None:
        """NEWS spells it "van den Berg", the filename squashes it."""
        assert u.bullet_ships("- Add van den Berg 2025 mab (link).", self.TOKENS)

    def test_multi_model_bullet_is_never_dropped(self) -> None:
        """The regression: this covers 14 models and names no single author."""
        bullet = (
            "- Add 14 published imatinib population PK models transcribed from "
            "the Yang 2025 external evaluation (link)."
        )
        assert u.bullet_ships(bullet, self.TOKENS)

    def test_star_marker_bullet_is_parsed(self) -> None:
        assert u.bullet_ships("* Add Smith 2020 drug (link).", self.TOKENS)
        assert not u.bullet_ships("* Add Jones 2021 other (link).", self.TOKENS)

    def test_non_add_bullet_kept(self) -> None:
        assert u.bullet_ships("- Fixed a typo in the vignette.", self.TOKENS)

    def test_no_tokens_means_no_gating(self) -> None:
        """No modeldb diff to gate on -- keep everything rather than drop all."""
        assert u.bullet_ships("- Add Jones 2021 other (link).", set())
