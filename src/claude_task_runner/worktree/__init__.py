"""Git-worktree housekeeping for queues whose hook creates a worktree per task.

Worktree *creation* stays the pre-dispatch hook's job (ADR-0013). This package
only *reclaims* the worktrees of finished tasks, and only when it can prove
nothing is lost (ADR-0034). See :mod:`claude_task_runner.worktree.reclaim`.
"""
