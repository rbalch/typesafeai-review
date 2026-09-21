# Review comparisons

One row per task run through `/orchestrate`, added by `review-judge` (RA-05) and
committed with the task's squash. Ground truth is the judge's own read of the diff, not
either review's self-report: see `.claude/agents/review-judge.md`.

## Columns

- **date** — the day the task was orchestrated.
- **task** — the task id (`T-03`, `RA-05`).
- **llm verdict / score** — `review.json`'s `verdict` and `score`.
- **ts verdict / score** — `ts-review.json`'s `verdict` and `score`.
- **agreed / llm-only / ts-only** — finding counts: agreed means both reviews flagged
  the same key and the judge's own labels support it; llm-only and ts-only mean only
  that review raised something the judge's labels back.
- **call** — the judge's one-line verdict on which review was more useful for this diff,
  and why.

| date | task | llm verdict/score | ts verdict/score | agreed | llm-only | ts-only | call |
|---|---|---|---|---|---|---|---|
