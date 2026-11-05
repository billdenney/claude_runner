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
├── real_2026-11-05_weekly_only.cap    # no 5-hour window: weekly 16%, Fable 12%
├── real_2026-11-05_session_and_weekly.cap  # 14% / 61%, Fable 0%
└── ...
```

An `.expected.json` with `"five_hour": null` is a panel without a 5-hour
window (ADR-0040). `extra_windows`, when present, lists the per-model
weekly sections; `usage parse-file` does not print them, so they are added
by hand.

The two `real_2026-11-05_*` captures come from `claude` 2.1.318 in an 80x24
pseudo-terminal, from `/usage` through the panel's own fetch:

- `weekly_only` is a plan with no 5-hour window. It is the whole stream of a
  capture that timed out while it waited for two `Resets` byte runs, so it
  holds no cleanup. Its panel opens at 15% under `Refreshing…` and the fetch
  draws 16% and the Fable section.
- `session_and_weekly` is a Team plan, cut where the capture sent Esc. Its
  panel opens with the saved session and weekly sections and gains the
  Fable section from the fetch. What Esc drew next is
  `tests/fixtures/usage_cleanup/real_2026-11-05_session_and_weekly_after_esc.cap`.

They were sanitised with same-length byte replacements, as the start-up
fixtures were: the queue directory's name became `runner_queue_example`.
They hold no token, e-mail address, organisation or account id.

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
