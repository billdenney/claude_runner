# Contributing to claude_runner

Thanks for your interest in improving claude_runner. This document covers the
development workflow and the conventions we use to keep the project easy to
maintain.

## Dev setup

```sh
git clone https://github.com/wdenney/claude_runner.git
cd claude_runner
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,api]"
pre-commit install
```

## Running checks locally

```sh
ruff check .
ruff format --check .
mypy
pytest
```

Pre-commit runs the first three automatically on staged files; `pytest` is
run by CI on every push.

## Decision log (ADRs)

Architectural decisions live under `docs/decisions/` as numbered Markdown files
in the MADR format. The template is `docs/decisions/0000-template.md`.

**Any change that affects the public CLI, the YAML task schema, the budget
algorithm, or the backend selection must add or supersede an ADR in the same
PR.** Copy `0000-template.md` to the next free number, fill it in, and link
it from `docs/decisions/README.md`. If the new decision replaces an older
one, mark the older ADR `superseded-by-NNNN` rather than editing its body.

The goal is that every load-bearing design choice has a short, dated page
explaining *why* — so six months from now a new contributor can answer the
"why is it done this way?" question without a git-archaeology expedition.

## Tests

- Unit tests live in `tests/` and run against a fake Agent SDK and a fake
  `ccusage` subprocess — no network calls, no real `claude` invocations.
- Use `time-machine` for anything that touches window math.
- If you add a feature, add at least one test for the golden path and one
  for a failure mode.
- The live smoke test (documented in `README.md`) is a manual step — not
  part of CI — because it costs real tokens.

## Commits & pull requests

- Keep PRs focused; split unrelated changes.
- Reference the ADR number in the PR description when the change is
  decision-driven.
- CI must be green before merge.
