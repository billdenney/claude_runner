"""No CHANGELOG.md until the first release.

The project has not had a release, so nothing reads a changelog yet, and
every branch in flight added its entry at the top of the same list: each
merge to ``main`` left every other open PR in conflict. On 2026-09-26 the
maintainer decided to drop the file until the first release. It is listed
in ``.gitignore``, but a branch that still edits it brings it back if it
merges ``main`` and settles the modify/delete conflict by keeping its copy.
This test catches that.

At the first release, delete this test and the ``CHANGELOG.md`` entry in
``.gitignore``, and start the changelog from that version.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def test_no_changelog_before_first_release() -> None:
    assert not (REPO_ROOT / "CHANGELOG.md").exists(), (
        "CHANGELOG.md is not kept until the first release. Remove it (after a "
        "modify/delete merge conflict: git rm CHANGELOG.md) and describe the "
        "change in the PR body instead."
    )
