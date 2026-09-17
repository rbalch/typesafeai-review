# Pipeline runs

One row per live `--record` run of the pipeline (spec §10: measure, never fabricate).
Fixtures produced land under `tests/fixtures/responses/`; the test suite only ever
replays them (`tests/test_pipeline.py`). A real-repo Manual QA row (T-10's own
Manual QA section) is added by a human after merge, not by the builder.

`wall_seconds` for the sample-repo rows below is the checks-step time (git worktree +
`uv run pytest -q`, dominant and reproducible) plus the ask-step network time from the
per-request `elapsed_ms` lines the live run logged to stderr; the two were not both
captured by one wall clock in the same invocation, so it is an estimate, not a
`time`-measured figure, and is noted as such.

| date | repo | hunks | requests | input_tokens | wall_seconds | verdict | notes |
|---|---|---|---|---|---|---|---|
| 2026-09-17 | tests/fixtures/repos/sample (`--task` given) | 2 | 3 | 5183 | ~1.3 (est.) | CHANGES_REQUESTED | First live `--record` run for T-10. `model=jev-1.13.0`. `hunks` is `slice_diff`'s own count (verified: `uv run python -c "..." ` printed `hunks: 2` -- `pkg.py` and `tests/test_thing.py`); `requests` is 3 because the fan-out sends one request per hunk plus one change-wide request. Flagged `swallows_exception` (0.91) and `missing_type_hints_public` (0.96) on the first try -- no re-record needed. Fixtures: `tests/fixtures/responses/pipeline_sample/`. |
| 2026-09-17 | tests/fixtures/repos/sample (`--task` omitted) | 2 | 3 | 4952 | ~1.2 (est.) | NEEDS_HUMAN | Same repo, no `--task`, to record the `stop_reason: missing_context` acceptance case. Same hunk/request split as above. `model=jev-1.13.0`. Fixtures: `tests/fixtures/responses/pipeline_sample_no_task/`. |
