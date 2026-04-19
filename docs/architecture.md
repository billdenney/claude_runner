# Architecture

```
 ┌───────────────────────────────────────────────────────────────┐
 │                         claude_runner run                     │
 └───────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌─────────────┐    ┌─────────────────────┐    ┌────────────────┐
 │ TodoCatalog │◀──▶│     Scheduler       │◀──▶│ CircuitBreaker │
 │ (~60s cache)│    │  (asyncio gather)   │    └────────────────┘
 └──────┬──────┘    └─────────┬───────────┘
        │                     │
        ▼                     ▼
 ┌─────────────┐    ┌─────────────────────┐
 │  StateStore │◀──▶│TokenBudgetController│──┐
 │  YAML files │    │ + RollingWindow 5h  │  │
 └──────┬──────┘    │ + WeeklyWindow 7d   │  │
        │           └─────────┬───────────┘  │
        │                     │              │
        │                     ▼              ▼
        │           ┌─────────────────────┐  ┌──────────────────┐
        │           │  BudgetSource       │  │ RunnerBackend    │
        │           │  (ccusage / ctx /   │  │ (asyncio /       │
        │           │   api_headers /     │  │  subprocess)     │
        │           │   static)           │  │                  │
        │           └─────────────────────┘  └────────┬─────────┘
        │                                             │
        ▼                                             ▼
 ┌─────────────┐                          ┌──────────────────────┐
 │EventEmitter │◀─────── Stop hook ──────│  claude_agent_sdk    │
 │ events.nd   │                          │  or `claude` CLI     │
 │ json        │                          └──────────────────────┘
 └─────────────┘
```

## Data flow for a single task

1. **Discovery** — `TodoCatalog.ready_tasks()` returns task specs whose
   state is not terminal, whose dependencies are satisfied, and that
   are not already in-flight. Results are cached for
   `discovery_cache_ttl_s` (default 60 s) but invalidated immediately
   on any state change.
2. **Gating** — `Scheduler` asks `TokenBudgetController.may_start(est)`.
   The controller returns `OK`, `WAIT(until)`, or `STOP` based on the
   5-hour window, the weekly window, the weekly guard (≤90% until the
   last day), and the in-flight reservation.
3. **Dispatch** — On `OK`, the scheduler reserves tokens and hands the
   spec to the configured backend (`AsyncioBackend` by default). The
   backend starts a `claude_agent_sdk.query()` coroutine (or spawns
   `claude -p`) with the captured `session_id` on `resume=` if one
   exists.
4. **Session capture** — The first `SystemMessage(subtype="init")`
   carries a `session_id`, which is persisted to
   `.claude_runner/state/<id>.yaml` synchronously so a `kill -9` is
   recoverable.
5. **Completion** — The Agent SDK's `Stop` hook fires; our hook (in
   `runner/stop_hook.py`) writes the final status, stop reason, and
   error (if any) to the state file, and appends a `task_completed`
   or `task_failed` event to `.claude_runner/events.ndjson`.
6. **Accounting** — The backend returns a `DispatchResult`. The
   scheduler updates the EMA, releases the reserved budget, records
   success/failure to the circuit breaker, and invalidates the
   catalog cache so the next `ready_tasks()` call reflects reality.

## Module responsibilities

| Module | Responsibility |
|--------|----------------|
| `config.py` | Load `claude_runner.toml` + env vars into a typed `Settings` |
| `defaults.py` | Effort-tier table + plan presets — the single place knobs live |
| `models.py` | Plain dataclasses: `Task`, `TaskState`, `TokenUsage`, `DispatchResult` |
| `todo/schema.py` | Pydantic model + default resolution for a task YAML |
| `todo/loader.py` | One-shot `load_todo_dir` used by `validate` |
| `todo/catalog.py` | The dynamic, cached view the scheduler talks to |
| `state/store.py` | Atomic per-task YAML read/write under `.claude_runner/state/` |
| `state/lock.py` | `fcntl`-based per-task advisory lock |
| `budget/windows.py` | Pure window accounting (no I/O) |
| `budget/circuit_breaker.py` | Failure tracking + trip logic |
| `budget/controller.py` | Concurrency sizing + `OK/WAIT/STOP` decisions |
| `budget/sources/*` | `ccusage`, `claude /context`, and API header probes |
| `runner/scheduler.py` | The main asyncio loop — wires everything together |
| `runner/asyncio_backend.py` | Drives `claude_agent_sdk.query()` |
| `runner/subprocess_backend.py` | Drives `claude -p --output-format stream-json` |
| `runner/stop_hook.py` | The Stop hook factory per task |
| `notify/emitter.py` | Append NDJSON events to `events.ndjson` + per-task log |
| `scaffold.py` | Template rendering for `init` and `new` |
| `cli.py` | `argparse` entry points |

Each module has a narrow surface and an obvious seam for testing —
`RunnerBackend` and `BudgetSource` are `Protocol`s, so the test suite
injects fakes rather than mocking the real SDK or shelling out.
