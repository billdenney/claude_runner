# Start-up screen fixtures for the usage capture

Raw PTY bytes (ANSI included) of `claude` 2.1.318 starting in an 80x24
pseudo-terminal, the size the usage capture spawns it at. They are the
input of `tests/unit/test_capture_tui.py`, which feeds them to the
capture's phase 1 (wait for the input box, answer prompts, read the
banner's billing label) through a stand-in for the pexpect child.

They are not parser fixtures: none of them reaches `/usage`, so they have
no `.expected.json` and live outside `tests/fixtures/usage/`, whose every
`.cap` the parser tests replay.

| File | Screen |
|---|---|
| `real_2026-11-05_manual_mode.cap` | manual permission mode, footer `⏸ manual mode on · ? for shortcuts`; not logged in |
| `real_2026-11-05_accept_edits_mode.cap` | the stream above after one shift+tab: `⏵⏵ accept edits on (shift+tab to cycle)` |
| `real_2026-11-05_plan_mode.cap` | after a second shift+tab: `⏸ plan mode on (shift+tab to cycle)` |
| `real_2026-11-05_auto_mode_claude_api.cap` | auto mode (the default), banner `Sonnet 5.5 · Claude API`: a `setup-token` token in `CLAUDE_CODE_OAUTH_TOKEN` |
| `real_2026-11-05_auto_mode_not_logged_in.cap` | auto mode, banner `Opus 5.5 · API Usage Billing` and `Not logged in · Run /login`: an expired `.credentials.json` |
| `real_2026-11-05_fullscreen_upsell.cap` | the "Try the new fullscreen renderer?" dialog, `❯ 1. Yes, try it` focused |
| `real_2026-11-05_fullscreen_upsell_after_esc.cap` | what `claude` drew after Esc on that dialog: the input box, auto mode |
| `real_2026-11-05_trust_prompt.cap` | "Quick safety check" for an untrusted directory, `❯ No, exit` focused |
| `real_2026-11-05_trust_prompt_after_down.cap` | what Down drew: the focus on `Yes, I trust this folder` |
| `real_2026-11-05_trust_prompt_after_enter.cap` | what Enter then drew: the input box, auto mode |
| `real_2026-11-05_theme_picker.cap` | the first-run theme picker of a config dir without `hasCompletedOnboarding`, `Dark mode` focused |
| `real_2026-11-05_theme_picker_after_enter.cap` | what Enter drew: "Select login method", the next first-run screen of a config dir with no login |

Each `*_after_*` file continues the stream of the file it is named after,
and renders only on top of it.

All but the two auto-mode captures come from scratch config dirs whose
`.claude.json` held the onboarding flag and a trust entry for the directory
`claude` started in, except that the trust prompt's had no trust entry for
it and the theme picker's no onboarding flag. The fullscreen dialog was forced with `CLAUDE_CODE_FORCE_FULLSCREEN_UPSELL=1`.
The two auto-mode captures come from real account config dirs and end
before `/usage` was typed.

Sanitised with same-length byte replacements, so cursor positions still
line up: the scratch directory's session id became zeros, the user name in
its path `user`, and the queue directory's name `runner_queue_example`
(`runner-queue-example` where the path is slugged). No token or account
name appears in any file.
