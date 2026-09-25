# Queue YAML fixtures

Task and state YAMLs read by `tests/unit/test_store.py`. `TestLoaderParity`
loads every file with both of the loaders `queue/store.py` can select,
LibYAML's `CSafeLoader` and PyYAML's pure-Python `SafeLoader`, and requires
identical results. Each file is also re-encoded four ways (as-is, CRLF line
endings, UTF-8 BOM, UTF-16LE BOM). `TestQueueFixtures` pins known answers so
that a fixture which quietly stopped exercising its path would fail rather
than make the parity test pass vacuously.

## Layout

```
tests/fixtures/queue/
├── README.md               # this file
├── tasks/
│   ├── minimal.yaml        # the three required fields only
│   ├── runner_written.yaml # write_task_atomic output, every Task field set
│   └── hand_authored.yaml  # comments, block scalars, anchors/aliases, a merge
│                           # key, YAML 1.1 booleans and digit grouping, raw
│                           # non-ASCII, %YAML directive and document markers
└── states/
    ├── pending_minimal.yaml  # task_id only
    ├── runner_written.yaml   # write_state_atomic output, every TaskState,
    │                         # RunRecord and TokenUsage field set
    └── hand_edited.yaml      # unquoted timestamps (resolved to datetime by
                              # the YAML 1.1 resolver), folded and keep-chomped
                              # block scalars, flow mappings
```

## Adding a fixture

Register the file in `FIXTURE_LOADERS` in `tests/unit/test_store.py`;
`test_fixture_registry_matches_disk` fails until the registry and this
directory agree.

## When the schema gains a field

`test_runner_written_fixtures_populate_every_field` enumerates the models'
fields and fails until both `runner_written.yaml` files carry the new one.
Add it where the writer would put it (fields are written in model order, as
`write_task_atomic` / `write_state_atomic` emit them), so the parity test
covers the new field too.
