# Usage capture fixtures

Each `.cap` file is a raw capture of `claude /usage` output (including ANSI
escape sequences). Tests in `tests/unit/test_parser.py` parse every `.cap`
and assert against its sibling `.expected.json`.

## Layout

```
tests/fixtures/usage/
├── README.md                          # this file
├── synthetic_normal.cap               # 38% / 20% — typical use
├── synthetic_normal.expected.json
├── synthetic_high_5h.cap              # 92% / 47% — 5h near limit
├── synthetic_high_5h.expected.json
├── synthetic_weekly_capped.cap        # 50% / 91% — weekly paused
├── synthetic_weekly_capped.expected.json
├── synthetic_zero.cap                 # 0% / 0% — fresh / unused
├── synthetic_zero.expected.json
└── ...
```

The `synthetic_*.cap` files are hand-crafted to exercise the parser's
ANSI-stripping and block-extraction paths. They are deliberately
representative of the documented TUI output, but byte-perfect recordings
from real `claude` are preferred when available.

## Adding a new fixture (real capture)

```sh
claude-task-runner usage capture --save tests/fixtures/usage/<YYYYMMDD>_<label>.cap
claude-task-runner usage parse-file tests/fixtures/usage/<YYYYMMDD>_<label>.cap > \
    tests/fixtures/usage/<YYYYMMDD>_<label>.expected.json
```

Inspect the resulting `.expected.json` for sanity, commit both files.

## Marking a fixture as permanent

Fixture rotation (`scripts/rotate_fixtures.py`) drops `.cap` files older
than `[fixtures].rotation_window_days` (default 30) UNLESS a sibling
`<name>.cap.keep` marker file exists. Mark fixtures as permanent when
they encode a specific format variant we want to regression-test forever:

```sh
touch tests/fixtures/usage/<name>.cap.keep
git add tests/fixtures/usage/<name>.cap.keep
```
