# Run anywhere — spec

**Status:** agreed 2026-09-18 · **Extends:** `typesafe-reviewer.md` (§3.1, §9) · **Prefix:** `RA`

## Goal

`ts-review` runs on any branch, commit, range or PR of any repo on this machine with one
command and no shell ceremony: the key comes from a config file the tool finds itself,
the task brief comes from the PR when there is no task file, and the tool makes its own
temporary worktree when it needs one. Every run inside this repo's orchestrate loop
also produces a labelled calibration case, judged by a new `review-judge` agent that
compares the LLM reviewer's `review.json` with `ts-review.json`. The LLM reviewer stays
the control group; `ts-review` is observational until the judge's record says otherwise.

## Approach

```
--env-file / <worktree>/.env / cwd..root .env / ~/.config/typesafe-review/env
        │  (TYPESAFE_* only, never overrides a set var, base URL normalised)
        ▼
--task <file|N|#N|url> ──▶ taskfile.parse_task ◀── prsource.extract_brief(gh pr view)
--pr N / --ref / --commit / --range ──▶ resolve (base_sha, head_sha)
        │  temp detached worktree at head_sha, removed in finally
        ▼
checks → slice(base_sha...HEAD) → ask → compose → render ──▶ --out <dir> (default: source repo root)
        └──▶ --case <dir>: state/ + keys.json + responses/ + review copies + meta.json
                                   └── review-judge writes labels.json  ──▶ fixtures/real/<id>/
                                       and a row in docs/review-comparisons.md
```

Seams unchanged: verdict and score still come from `compose.py` tables; the model only
answers questions; the judge is an LLM agent in `.claude/`, not Python, and its output
is labels and a comparison row, never review text.

## Out of scope

- A real `APPROVE` / `CHANGES_REQUESTED` without task and red sha. Ad-hoc runs stay
  `NEEDS_HUMAN / missing_context` with findings, score and both files written (already
  true today).
- Porting to `new-project`. Done by hand on a `ts-review` branch there once the judge
  record shows the tool is worth a toggle. The toggle itself is that port's job.
- Generating a task file for a foreign repo with an LLM (a `draft-task` skill). Deferred
  until a PR with no brief in its body actually needs reviewing.
- Threshold changes. `fixtures/` grows first; `docs/runs.md` records any edit.
- Gating the orchestrate loop on `ts-review`'s exit code.

## Decisions

- `.env` loading reads only `TYPESAFE_*` keys and never overrides a set variable, so a
  target repo's own `.env` cannot leak unrelated secrets into the run.
- `TYPESAFE_BASE_URL` is normalised (trailing `/v1/systemone` stripped, stderr warning)
  because the SDK appends the path itself (ledger H-4).
- `--doctor` is a flag, not a subcommand; the CLI stays flag-based.
- Task brief from a PR is a deterministic parse of the `<details><summary>Task brief …`
  block orchestrate writes into every PR body. No LLM.
- Red sha from a PR body is best-effort (`Red: <sha>` or `red-then-green on <sha>`),
  verified with `git cat-file -e`, and its source is recorded in `ts-review.json`.
- Ref modes always run in a temporary detached worktree the tool owns and removes.
  The user's checkout is never touched.
- Real-run fixture cases store dumped state, not `before/`/`after/` trees, and live
  under `fixtures/real/`. Calibrate accepts both kinds.
- Comparison is recorded twice on purpose: `labels.json` in this repo's `fixtures/real/`
  (calibration data) and a row in the target's `docs/review-comparisons.md` (the
  human-readable trail). Here the target is this repo, so both land in one tree.
- Dogfood in this repo's `.claude/` first. No ADR: no alternative was rejected that a
  reader would re-litigate.

## Tasks

| id | title | depends_on |
|---|---|---|
| RA-01 | Load `.env` from known places; `--doctor` | — |
| RA-02 | `--task` accepts a PR number or URL | — |
| RA-03 | Review any ref: `--pr`, `--ref`, `--commit`, `--range`, `--out` | RA-02 |
| RA-04 | `--case` writes a real-run fixture; calibrate reads it | — |
| RA-05 | `review-judge` agent and the dogfood loop | RA-04 |

Every task but RA-05 edits `cli.py`, so RA-01 → RA-02 → RA-03 → RA-04 run in order in
one session. RA-05 touches only `.claude/`, docs and `.gitignore`; it waits for RA-04
to merge because its brief invokes `--case`.

The merge-base fix (PR #14) precedes RA-03 and is not a task.

## Open questions

- **PR body format.** `Red: <sha>` as a structured line belongs in the orchestrate
  skill's PR template. RA-05 adds it here; the `new-project` port carries it over.
- **`gh` in foreign repos.** RA-02 assumes `gh auth status` passes for the repo's
  remote. A repo with no GitHub remote gets a clear exit 1, nothing more.
- **Cost of `make check` in a temp worktree.** A fresh worktree may resolve a new
  `.venv`. Measure on the first `--pr` run and note it in `docs/runs.md`. Owner: Ryan.
