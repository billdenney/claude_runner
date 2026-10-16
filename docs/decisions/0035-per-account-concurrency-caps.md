# ADR-0035: Per-account caps are the only concurrency limit by default

- **Date:** 2026-10-15
- **Status:** accepted
- **Related:** ADR-0011 (its 2026-09-25 update describes `[concurrency]`),
  ADR-0022 (`[dispatch_pct.*]` and the per-account ramp), ADR-0024
  (multi-account dispatch)

## Context

Two settings capped how many tasks run at once:

- Each account's `max_concurrency`, in `<config_dir>/runner-account.toml`
  (1 when the file or key is missing). The supervisor re-reads it every
  tick, and `runner.account_dispatch.choose_account` holds each account at
  it, lowered to the account's throttle ramp target.
- The queue TOML's `[concurrency]`: `initial_concurrency` until a first
  task completes, then `max_concurrency`, as a ceiling on the queue's
  in-flight tasks across all accounts
  (`runner.orchestrator._target_concurrency`). Both keys were required, and
  the packaged defaults set them to 1 and 2.

On the live queue the accounts' caps were `work` 3 and `personal` 5, 8 in
all, but `[concurrency].max_concurrency = 5` held the total at 5, so
`personal` only ever got 2 slots. Nothing said so: no log line, and
`account list` showed `personal` with free slots, so the operator had to
discover the cap. They asked: "Make the limit not 5 queue-wide but each
account can have its own maximum limit."

Making each account's file the only limit also made a broken file matter
more. `config.loader.resolve_accounts` raised on the first account whose
`runner-account.toml` did not parse or validate, and nothing caught it:
the supervisor's tick loop died on the next tick, `account list` printed a
traceback, and force-dispatch failed, all because of one account. The
operator asked to "make failing account files fail loud but not fatal".

The queue-wide value duplicated the per-account caps and had to be kept in
step with them by hand. An account owner who raised their own cap got
nothing, and a queue that relied on the packaged defaults was capped at 2
across all accounts without its operator having written the number down.

## Decision

1. **Both `[concurrency]` keys are optional**, and the packaged
   `settings.toml` no longer sets them. Each still rejects 0, negative
   and non-integer values.

2. **Dispatch counts free slots per account.** A tick may start
   `runner.account_dispatch.dispatch_headroom` tasks: each declared
   account's free slots under its own `max_concurrency`, summed. The caps
   come from the accounts `config.loader.resolve_accounts` returned on the
   same tick, the resolution `choose_account` caps each account by, so an
   edit to an account's `runner-account.toml` takes effect within a tick,
   with no SIGHUP and no change to the queue TOML. An account running more
   than its cap, because the cap was lowered while its tasks ran, adds 0
   rather than taking the others' slots, and the tasks of an account no
   longer in `[[accounts]]` count against no account. With the
   `[concurrency]` keys left out, no account is ever held below its own
   cap. A single queue-wide number (`max_concurrency`, else the sum of the
   caps, minus every in-flight task) would not hold that: with `personal`
   lowered from 5 to 2 while 5 of its tasks run, the sum is 5 with 5 in
   flight, and `work`'s 3 free slots would stay empty until `personal`
   drained.

3. **`initial_concurrency`** keeps its meaning when set: the cap until a
   first task completes in the queue. Left out, there is no warm-up cap,
   and the orchestrator does not scan `state/` for a completion.

4. **An explicit `max_concurrency` is still honoured as a global cap.**
   Every in-flight task counts against it, declared account or not, on top
   of the per-account count in 2. One below the accounts' sum binds, and
   the supervisor logs a WARNING naming both numbers and each account's
   cap. It logs it on the first tick that binds after a start, again on the
   first tick after each successful SIGHUP reload, and again whenever
   either number changes while it binds, such as an account's file edited
   so that the cap starts to bind. Not on every tick. A failed reload
   keeps the old config and does not repeat the warning.

5. **The ceiling is reported.** `runner.account_dispatch.QueueCeiling`
   summarises the numbers in 2 and 4: the most tasks the accounts can run
   at once, which is an explicit `max_concurrency` when it binds and the
   accounts' sum otherwise (an explicit 10 over a sum of 8 reports 8),
   where that number comes from, and whether the explicit value binds.
   - The supervisor logs it at INFO (`concurrency ceiling: ...`)
     once at start and once after each successful SIGHUP reload, whether
     or not it binds, so a queue whose limit changed with this decision
     says so in its log.
   - `account list` and `supervisor status` print it, plus
     `initial_concurrency` when set. Their `--json` output carries a
     `queue_ceiling` object (`ceiling`, `max_concurrency`,
     `accounts_total`, `account_caps`, `binds`, `broken_accounts`,
     `initial_concurrency`). Both read the files on disk, so a change to
     `claude_runner.toml` shows there before the supervisor's next SIGHUP.
     An account whose file does not load (9) is left out of the sum and
     named in the line and in `broken_accounts`; `supervisor status` also
     prints an `Account error:` line for it and lists it under
     `account_errors` in `--json`.

6. **Force-dispatch** without `--over-limit` waits for a free slot. With an
   explicit `max_concurrency`, that is every in-flight task counted against
   it, as before. Without one, it waits until some account has a free slot
   under its own cap. Neither uses the warm-up cap. The request goes to its
   affined or pinned account; an unbound one goes to the first account, in
   declaration order, with a free slot under its own cap, and to the first
   account only when none has one, which only `--over-limit` or an
   explicit `max_concurrency` lets through. The daemon's policy for a
   usage reading it cannot attribute to an account carries the reported
   ceiling, with an account whose file does not load counting 0.

7. **Queues that set both keys** dispatch exactly as before: the
   per-account count in 2 never admits more than `choose_account` would
   have. **Queues that set one key** change, because the packaged default
   that filled the other (`initial_concurrency = 1`, `max_concurrency = 2`)
   is gone:
   - Only `max_concurrency = N`: there is no warm-up cap, so up to N tasks
     run from the first tick, where before only 1 ran until a first task
     completed.
   - Only `initial_concurrency = N`: N until a first task completes, then
     the accounts' sum, where before it was 2.

8. **Single-account queues** (one `[[accounts]]` entry, or the `default`
   account synthesised from `[claude].config_dir`) follow the same rules.
   Left out, the limit is that one account's cap: 1 unless its
   `runner-account.toml` raises it. An empty `config_dir` has no such file,
   so its account stays at 1.

9. **An account whose file does not load takes no new tasks, and nothing
   stops because of it.** `config.loader.resolve_accounts` never raises
   for a per-account file. It returns the accounts whose policy loaded (an
   absent file, or a `config_dir` that does not exist, still means the
   defaults) and, separately, a `BrokenAccount` with the file and the error
   for each one that is there but does not load: it cannot be read, is
   empty (zero bytes), is a symlink to nothing, is not UTF-8 TOML, sets the
   retired `[throttle.*]` or fails validation. A `config_dir` whose
   leading `~user` names no user is broken too. An empty file or a
   dangling symlink is most often a file caught mid-save, and reading it as
   "use the defaults" would silently give the account a cap of 1 and the
   queue's bands. Every caller uses that one path:
   - Dispatch leaves the account out: no new task goes to it, it adds 0
     free slots and 0 to the reported ceiling, and every other account
     dispatches as usual. Its running tasks keep running and are reaped as
     usual. A usage reading for it makes no throttle decision, since its
     bands are unknown; only its capture time moves, so the round robin
     goes on.
   - No policy stands in for the file: neither the defaults nor the last
     policy that loaded. A half-edited file must not be read as something
     its owner did not write, and a guessed `max_concurrency` could put
     more tasks on the account than its owner allows.
   - The supervisor logs an ERROR naming the account and the error (which
     names the file), once, and again only when the error or the file
     changes. While no account loads, one more ERROR says nothing is
     dispatched. When the file loads again, the next tick restores the
     account and logs one INFO line. This holds at startup as well: a
     supervisor started with a broken file starts and disables that
     account. The queue's own `claude_runner.toml` failing to load is
     still fatal.
   - `supervisor.json` (schema v8) records the error in the account's
     `policy_error`. `supervisor status` prints it and lists it under
     `account_errors` in `--json`. `account list` prints it in place of the
     account's policy, lists the others as usual, and exits 1. The doctor's
     `account_policies` check fails with it.
   - A force-dispatch affined or pinned to the account waits (the request
     stays) rather than going to another account; the synchronous path
     refuses with the error. A task whose session or pin is on the account
     is skipped with a reason that names the file's error, not as an
     unknown account.
   - When the file loads again, the INFO line says so; when it was deleted
     instead, it says the defaults now apply.

## Alternatives considered

- **Keep both keys required and only warn when they bind.** Makes the
  hidden cap visible but keeps the duplicate setting that must track the
  accounts' caps by hand, which is what the operator asked to be rid of.
- **Retire `[concurrency]`, rejecting it like the keys in
  `config.loader._RETIRED_KEYS`.** Every queue that sets it would stop
  loading, and a global cap still has a use that per-account caps do not
  cover: limiting the host's load (CPU, memory, disk per worker) whatever
  the accounts allow.
- **One queue-wide number for dispatch, the sum of the caps when
  `max_concurrency` is left out.** Simpler, but it counts the tasks of an
  account over its own cap, or of an account no longer declared, against
  the others: the shortfall in 2.
- **Fill an omitted `max_concurrency` with the sum when the settings
  load.** The sum would then change only on SIGHUP or restart, while each
  account's own cap changes on the next tick, so the two would disagree
  after any edit to an account's file.
- **Warn on every tick that binds.** A deliberate global cap would write
  one warning per poll interval for as long as it is set.
- **Keep using the last policy that loaded, or the defaults, while a file
  is broken.** The account would keep working, but on numbers its owner
  did not write: a cap they were lowering when they made the typo, or a
  default that ignores their bands. Stopping new work on that account
  only, loudly, is the smaller surprise.
- **Exit on a broken account file, as before.** One account's typo would
  stop every account's dispatch and leave the watchdog restarting a
  supervisor that dies on its next tick.
- **Warn when `initial_concurrency` binds.** It is a warm-up cap that ends
  at the first completion by design; the CLI shows it instead.

## Consequences

- (+) Each account's owner controls how many tasks run through it, and a
  change takes effect within a tick.
- (+) A global cap that holds accounts below their own caps is reported in
  the log and by the CLI, instead of being found by reading the code.
- (+) Lowering one account's cap, or taking an account out of
  `[[accounts]]` while its tasks run, no longer holds the other accounts
  below their caps.
- (-) A queue that relied on the packaged defaults (1 until the first
  completion, then 2) now runs up to the sum of its accounts' caps from the
  start. For accounts left at the default cap of 1 that is one task per
  account, as before for a single account. Its log says so: the INFO line
  at start names the ceiling and where it comes from. A queue that wants
  the old numbers sets them in `[concurrency]`.
- (-) A queue that set only one of the two keys changes as in 7.
- (-) Force-dispatch on a queue without an explicit `max_concurrency`
  waits for a free slot on some account, not for fewer than 2 tasks in
  flight.
- (+) A broken account file stops only that account, and says so in the
  log, `supervisor.json`, the CLI and the doctor.
- (-) `account list` exits 1 while any account's file does not load, which
  a script that only checked for success will notice.
- (neutral) `account list --json` and `supervisor status --json` gain a
  `queue_ceiling` key, `account list --json` rows gain `policy_error`, and
  `supervisor status --json` gains `account_errors`; existing keys are
  unchanged. `supervisor.json` moves to schema v8, which an older
  supervisor refuses to load.

## Reversibility

High for the concurrency rules. A queue that sets `[concurrency]
max_concurrency = 2` and `initial_concurrency = 1` dispatches exactly as
before, and its log says so, with a warning whenever 2 is below its
accounts' sum. Restoring the two values to the packaged `settings.toml`
restores it for every queue that leaves them out.

Medium for the code, because of `supervisor.json` schema v8. A build from
before this decision refuses a file with `schema_version = 8`, so after a
rollback `supervisor start` fails (and a watchdog keeps restarting it),
and `account list` and `supervisor status` fail too. Two remedies, with
the supervisor stopped:

- Edit the file back to v7: set `schema_version` to 7 and delete the
  `policy_error` key from every entry under `accounts` (it is `null`
  unless an account's file was broken). Nothing else changed in v8, so
  nothing is lost.
- Or move `supervisor.json` aside. The next start writes a fresh
  snapshot: every account starts in `no_reading` and is read again within
  one capture cycle, and the scheduled wakeups and the last readings,
  targets and drift messages are gone. Pauses survive, since they live in
  the `account_paused/` markers. The previous supervisor's `in_flight`
  list is lost, so a worker still running is adopted under its session's
  account, or no account when it has none (a first attempt), until it
  finishes.

## Amendment (2026-10-16) — a broken account file stops the supervisor from starting

**Decision change.** Decision 9 said a supervisor started with a broken
account file starts and disables that account. The operator revisited it:
"a broken account file should stop the supervisor from starting so that it
will be immediately seen and fixed", and a `config_dir` that cannot be
expanded should stop it too. A disabled account at startup is easy to
miss; a supervisor that does not start is not. The split is now:

- **At start, fatal.** `supervisor start` reads every declared account's
  `runner-account.toml` through `config.loader.resolve_accounts`, the same
  path as everywhere else, right after the queue TOML loads and before it
  creates the queue's runtime directory, takes the lock, adopts workers,
  writes `supervisor.json` or captures usage. When any account is broken
  it prints every one, with its file and error, and exits 78
  (`config.loader.CONFIG_ERROR_EXIT_STATUS`, `EX_CONFIG` in sysexits.h). A
  queue TOML that does not load now exits 78 the same way, with its error
  printed instead of a traceback.
- **While running, non-fatal, as decision 9 says.** A file that breaks
  after the supervisor started disables only its account, with the ERROR,
  `policy_error`, CLI and doctor reporting and the INFO line on recovery.
  Stopping every account's work for one account's typo is still the wrong
  trade once work is running. `start_daemon` itself still tolerates a
  broken file from its first tick, for one that breaks between the check
  and that tick.
- **Unknown `~user`.** `config.schema.AccountSettings` refuses a
  `config_dir` whose leading `~user` names no user, so the queue TOML does
  not load: `supervisor start` exits 78 naming the account, and a SIGHUP
  reload carrying one is rejected and the old config kept. A rejected
  reload, for this or any other reason, is now logged as an ERROR rather
  than a WARNING, since the operator's edit is not in force. Every later
  expansion of a `config_dir` goes through `config.paths.expand_config_dir`,
  which raises `ConfigDirError` instead of `RuntimeError`; each caller turns
  it into its own error (an account broken in `resolve_accounts`, a usage
  capture spawn error, missing API credentials, a dispatch error, a doctor
  failure), so a user removed while the supervisor runs fails that account
  and cannot end the tick loop.
- **No crash loop.** The systemd unit now has
  `RestartPreventExitStatus=0 78`: a refused start leaves the unit
  `failed`, with the reason in the journal, instead of being restarted
  until `StartLimitBurst` stops it. The cron watchdog reads the account
  files before a restart, as `supervisor start` would; when one does not
  load it logs an ERROR naming each, starts nothing, counts no restart and
  exits 1, as it already did for a queue TOML that does not load. It
  leaves a running supervisor alone.

**Consequences.** (+) A broken file or an unknown home at start is seen at
once, in the journal or `supervisor.log`, and the unit or watchdog stops
retrying. (-) An account file left broken across a restart (a reboot, a
deploy) now keeps every account from running until it is fixed or
deleted. (neutral) An existing unit file keeps `RestartPreventExitStatus=0`
until `claude-task-runner install` rewrites it or the line is edited.
