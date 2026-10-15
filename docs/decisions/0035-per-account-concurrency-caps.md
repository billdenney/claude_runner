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

The queue-wide value duplicated the per-account caps and had to be kept in
step with them by hand. An account owner who raised their own cap got
nothing, and a queue that relied on the packaged defaults was capped at 2
across all accounts without its operator having written the number down.

## Decision

1. **Both `[concurrency]` keys are optional**, and the packaged
   `settings.toml` no longer sets them. Each still rejects 0, negative
   and non-integer values.

2. **The ceiling is `runner.account_dispatch.QueueCeiling`.** It is
   `[concurrency].max_concurrency` when the queue TOML sets it, and
   otherwise the sum of the accounts' own `max_concurrency`. The sum comes
   from the accounts `config.loader.resolve_accounts` returned on the same
   tick, the resolution `choose_account` caps each account by, so an edit
   to an account's `runner-account.toml` moves both within a tick, with no
   SIGHUP and no change to the queue TOML. Left out, the ceiling never
   binds: no account can run more than its own cap, so the sum is never
   reached early.

3. **`initial_concurrency`** keeps its meaning when set: the cap until a
   first task completes in the queue. Left out, there is no warm-up cap,
   and the orchestrator does not scan `state/` for a completion.

4. **An explicit `max_concurrency` is still honoured as a global cap.**
   One below the accounts' sum binds, and the supervisor logs a WARNING
   naming both numbers and each account's cap. It logs it on the first
   tick that binds after a start, again on the first tick after each
   successful SIGHUP reload, and again whenever either number changes while
   it binds, such as an account's file edited so that the cap starts to
   bind. Not on every tick. A failed reload keeps the old config and does
   not repeat the warning.

5. **The CLI shows the ceiling.** `account list` and `supervisor status`
   print it with its source and whether it binds, plus
   `initial_concurrency` when set; their `--json` output carries a
   `queue_ceiling` object (`ceiling`, `max_concurrency`, `accounts_total`,
   `account_caps`, `binds`, `initial_concurrency`). If an account's file
   does not load, `supervisor status` reports the error in its place.

6. **Force-dispatch** without `--over-limit` waits for a slot under the same
   ceiling, without the warm-up cap, as it did under `max_concurrency`.
   The daemon's policy for a usage reading it cannot attribute to an
   account carries the ceiling too.

7. **Single-account queues** (one `[[accounts]]` entry, or the `default`
   account synthesised from `[claude].config_dir`) that set the keys
   behave exactly as before. Left out, the ceiling is that one account's
   cap: 1 unless its `runner-account.toml` raises it. An empty
   `config_dir` has no such file, so its account stays at 1.

## Alternatives considered

- **Keep both keys required and only warn when they bind.** Makes the
  hidden cap visible but keeps the duplicate setting that must track the
  accounts' caps by hand, which is what the operator asked to be rid of.
- **Retire `[concurrency]`, rejecting it like the keys in
  `config.loader._RETIRED_KEYS`.** Every queue that sets it would stop
  loading, and a global cap still has a use that per-account caps do not
  cover: limiting the host's load (CPU, memory, disk per worker) whatever
  the accounts allow.
- **Fill an omitted `max_concurrency` with the sum when the settings
  load.** The sum would then change only on SIGHUP or restart, while each
  account's own cap changes on the next tick, so the two would disagree
  after any edit to an account's file.
- **Warn on every tick that binds.** A deliberate global cap would write
  one warning per poll interval for as long as it is set.
- **Warn when `initial_concurrency` binds.** It is a warm-up cap that ends
  at the first completion by design; the CLI shows it instead.

## Consequences

- (+) Each account's owner controls how many tasks run through it, and a
  change takes effect within a tick.
- (+) A global cap that holds accounts below their own caps is reported in
  the log and by the CLI, instead of being found by reading the code.
- (-) A queue that relied on the packaged defaults (1 until the first
  completion, then 2) now runs up to the sum of its accounts' caps from the
  start. For accounts left at the default cap of 1 that is one task per
  account, as before for a single account. A queue that wants the old
  numbers sets them in `[concurrency]`.
- (-) Force-dispatch's ceiling on such a queue is the accounts' sum, not 2.
- (neutral) `account list --json` and `supervisor status --json` gain a
  `queue_ceiling` key; existing keys are unchanged.

## Reversibility

High. A queue that sets `[concurrency] max_concurrency = 2` and
`initial_concurrency = 1` gets the old behaviour exactly, with a warning
whenever 2 is below its accounts' sum. Restoring the two values to the
packaged `settings.toml` restores it for every queue that leaves them out.
