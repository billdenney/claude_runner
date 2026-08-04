"""A clean-exit run that leaves work uncommitted must not be 'completed'.

Regression cover for the failure mode that silently stranded 25 extracted
models across 7 worktrees on the nlmixr2lib queue: the worker wrote its
deliverable report (so ADR-0020's evidence gate passed) but never committed
the model files, and the task was marked completed and never re-dispatched.
"""

from claude_task_runner.runner.dispatcher import OutputEvidence


class TestOutputEvidence:
    def test_clean_skip_is_not_flagged(self) -> None:
        """A genuine skip: deliverable written, no commit, CLEAN worktree."""
        ev = OutputEvidence(
            has_commit=False, has_sidecar=False, has_deliverable=True, uncommitted=()
        )
        assert ev.any is True
        assert ev.left_work_uncommitted is False

    def test_uncommitted_work_is_flagged(self) -> None:
        """The bug: report written, models left untracked."""
        ev = OutputEvidence(
            has_commit=False,
            has_sidecar=False,
            has_deliverable=True,
            uncommitted=(
                "inst/modeldb/specificDrugs/Wada_2023_sparsentan.R",
                "vignettes/articles/Wada_2023_sparsentan.Rmd",
            ),
        )
        assert ev.any is True, "evidence gate alone still passes -- that is the bug"
        assert ev.left_work_uncommitted is True, "the new gate must catch it"

    def test_committed_work_with_clean_tree_is_fine(self) -> None:
        ev = OutputEvidence(
            has_commit=True, has_sidecar=False, has_deliverable=True, uncommitted=()
        )
        assert ev.any is True
        assert ev.left_work_uncommitted is False

    def test_commit_plus_leftover_product_still_flagged(self) -> None:
        """Committing some work does not excuse leaving the rest uncommitted."""
        ev = OutputEvidence(
            has_commit=True,
            has_sidecar=False,
            has_deliverable=True,
            uncommitted=("inst/modeldb/specificDrugs/Half_done.R",),
        )
        assert ev.left_work_uncommitted is True

    def test_no_output_at_all_still_fails_the_original_gate(self) -> None:
        ev = OutputEvidence(
            has_commit=False, has_sidecar=False, has_deliverable=False, uncommitted=()
        )
        assert ev.any is False

    def test_default_uncommitted_is_empty(self) -> None:
        """Existing constructions without the new field keep old behaviour."""
        ev = OutputEvidence(has_commit=True, has_sidecar=False, has_deliverable=False)
        assert ev.uncommitted == ()
        assert ev.left_work_uncommitted is False


class TestDeliverableExclusion:
    """The deliverable report itself may sit uncommitted in the worktree."""

    def test_only_the_deliverable_uncommitted_is_not_leftover_work(self) -> None:
        # _verify_output_evidence filters declared deliverables out before
        # constructing the evidence, so by this point `uncommitted` is empty.
        ev = OutputEvidence(
            has_commit=False, has_sidecar=False, has_deliverable=True, uncommitted=()
        )
        assert ev.left_work_uncommitted is False

    def test_deliverable_plus_real_product_is_still_leftover_work(self) -> None:
        ev = OutputEvidence(
            has_commit=False,
            has_sidecar=False,
            has_deliverable=True,
            uncommitted=("inst/modeldb/specificDrugs/Chawla_2023_gefapixant.R",),
        )
        assert ev.left_work_uncommitted is True
