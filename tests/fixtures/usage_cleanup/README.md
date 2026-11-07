# Cleanup fixtures for the usage parser

What `claude` drew after the usage capture's Esc closed the `/usage` panel,
continuing the stream of the parser fixture it is named after in
`tests/fixtures/usage/`. They are not parser fixtures themselves: they render
only on top of that stream, so they live outside `tests/fixtures/usage/`,
whose every `.cap` the parser tests replay.

| File | Continues |
|---|---|
| `real_2026-11-05_session_and_weekly_after_esc.cap` | `tests/fixtures/usage/real_2026-11-05_session_and_weekly.cap` |

`claude` 2.1.318 answers Esc with `Settings dialog dismissed` and redraws
the terminal's 24 rows from the top. Rendered at the parser's 200x100, the
panel's rows below those survive without the session section above them,
which is why the parser does not read a dismissed panel as one without a
5-hour window (ADR-0040).

Sanitised like the parser fixture it continues; it holds no token, e-mail
address, organisation or account id.
