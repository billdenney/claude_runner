#!/usr/bin/env bash
# Run mypy for the pre-commit hook in .pre-commit-config.yaml.
#
# A `language: system` hook runs its entry with the committer's PATH, which
# holds no mypy unless the dev virtualenv is activated, although pre-commit
# itself runs from that virtualenv. This finds the mypy that the dev extras
# installed (pip install -e '.[dev]'), in order: in the checkout's own
# `.venv`; in the main checkout's `.venv` when this is a linked worktree
# (`git worktree add`), which has no virtualenv of its own; on PATH (an
# activated virtualenv, CI). The virtualenvs come first because another
# mypy on PATH, from the system or pipx, has neither the pydantic plugin
# nor this project's stubs and would report errors that CI does not.
# Outside a git checkout only PATH is searched.
#
# Usage: scripts/run_mypy.sh [mypy arguments...]
#
# Exit codes: mypy's own; 127 when no mypy is found.

set -euo pipefail

venvs=()
if top="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    venvs+=("$top/.venv")
    common="$(git rev-parse --path-format=absolute --git-common-dir)"
    if [ "${common%/*}/.venv" != "$top/.venv" ]; then
        venvs+=("${common%/*}/.venv")
    fi
fi

for venv in ${venvs[@]+"${venvs[@]}"}; do
    if [ -x "$venv/bin/mypy" ]; then
        exec "$venv/bin/mypy" "$@"
    fi
done
if command -v mypy >/dev/null 2>&1; then
    exec mypy "$@"
fi

looked=""
for venv in ${venvs[@]+"${venvs[@]}"}; do
    looked+="$venv, "
done
echo "run_mypy.sh: no mypy found (looked in: ${looked}PATH); activate the virtualenv" \
    "holding the dev extras (pip install -e '.[dev]')" >&2
exit 127
