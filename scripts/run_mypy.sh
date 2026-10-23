#!/usr/bin/env bash
# Run mypy for the pre-commit hook in .pre-commit-config.yaml.
#
# pre-commit itself runs from the virtualenv it was installed in, but a
# `language: system` hook runs its entry with the committer's PATH, which
# holds no mypy unless that virtualenv is activated. This finds mypy, in
# order: on PATH (an activated virtualenv, CI); in the checkout's own
# `.venv`; in the main checkout's `.venv` when this is a linked worktree
# (`git worktree add`), which has no virtualenv of its own.
#
# Usage: scripts/run_mypy.sh [mypy arguments...]
#
# Exit codes: mypy's own; 127 when no mypy is found.

set -euo pipefail

if command -v mypy >/dev/null 2>&1; then
    exec mypy "$@"
fi

top="$(git rev-parse --show-toplevel)"
common="$(git rev-parse --path-format=absolute --git-common-dir)"
main_checkout="${common%/*}"
for candidate in "$top/.venv/bin/mypy" "$main_checkout/.venv/bin/mypy"; do
    if [ -x "$candidate" ]; then
        exec "$candidate" "$@"
    fi
done

echo "run_mypy.sh: no mypy on PATH, in $top/.venv or in $main_checkout/.venv;" \
    "activate the virtualenv holding the dev extras (pip install -e '.[dev]')" >&2
exit 127
