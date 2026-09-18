# Calibration fixtures (spec §9)

`uv run ts-review --calibrate fixtures/` replays every case directory here and prints
per-question precision, recall and F-beta at the catalog threshold, plus the
threshold (swept 0.05..0.95) that would maximise F-beta. See `calibrate.py` for the
implementation and `docs/specs/typesafe-reviewer.md` §9 for the method.

## Layout

One directory per case, directly under `fixtures/`:

```
fixtures/<case>/
  before/           file tree at the diff's base commit
  after/             file tree at the diff's head commit
  labels.json        hand-written ground truth
  task.md            optional: a planner task file, for cases that exercise
                      change-wide criterion_*_satisfied / criterion_*_tested ids
  responses/          recorded `system_one` responses, keyed by request hash
```

`calibrate.py` builds `before/`/`after/` into a throwaway two-commit git repo
(`testrepo.build_two_stage_repo`: commit `before`'s tree, replace it with `after`'s
tree, commit again) and calls `slice_diff` on it -- there is no patch-apply path. A
case with no `before/` directory diffs against an empty tree (a wholly new file); no
`after/` directory diffs to an empty tree (a wholly deleted file).

## `labels.json`

```json
{
  "<hunk_key>": {"question_id": true},
  "<change>": {"question_id": true}
}
```

`hunk_key` is `f'{hunk.path}@{hunk.header}'`, derived from the sliced `Hunk` itself --
never the pipeline's positional (`hunk-{i}`) or content-hash (`request_key`) keys, so
labels stay stable across a `slice_diff` that reorders or truncates unrelated hunks.
`"<change>"` is the fixed key for change-wide questions (`tests_fitted_to_code`,
`scope_creep`, `new_behaviour_untested`, `touches_high_risk`, and any `criterion_*` id
from `task.md`).

**Every question id `calibrate.py` expects for a key that is not listed is treated as
label `false`.** Labels are written positively: only assert `true` for a question id a
human reviewer would genuinely fire on that hunk or the change. This means a case's
`before`/`after` diff should be narrow enough that "everything else is false" holds by
construction -- the point of a small, single-smell diff per case, not a large one with
many plausible findings only some of which got a `true` entry.

An unlabelled `question_id` on a labelled key, or a `labels.json` key that matches
neither a hunk nor `"<change>"`, is a `CalibrateError` (exit 1): `calibrate.py`
validates every label against the question set `slice_diff` + `questions_for` actually
produce for that case, before it ever needs `responses/` to exist.

## Recording `responses/`

Recording is a one-off live pass, per case, offline after that. From the root
checkout (not a worktree -- the key lives in its `.env`, which is not copied into a
worktree):

```bash
set -a; source <repo root>/.env; set +a
export TYPESAFE_BASE_URL="${TYPESAFE_BASE_URL%/v1/systemone}"
uv run python -c "
from pathlib import Path
from typesafe_review.calibrate import record_one_case
record_one_case(Path('fixtures/<case>'))
"
```

Never echo, print, or commit the key. Scrub any secret-shaped substring out of a
recorded state before committing, with the same redaction pattern `checks.py` uses
(`(?i)(token|secret|key)\s*[=:]\s*\S+` -> `<redacted>`) -- recorded response bodies
are Jev's *answers*, not the state sent, so in practice there is nothing to scrub in
the seed cases here, but a future case built from a real diff must be checked.

## The seed set

Eight cases, one smell each, chosen narrow so the "unlabelled == false" convention
above holds:

| case | targets |
|---|---|
| `swallows_exception` | `swallows_exception`, `bare_except` (a `except Exception: pass` is honestly both shapes) |
| `bare_except` | `bare_except`, `swallows_exception` (a bare `except:` with no re-raise) |
| `indistinguishable_default` | `indistinguishable_default` (a missing env var defaults to `''`) |
| `missing_type_hints_public` | `missing_type_hints_public` |
| `clean_change` | none -- all labels false (a docstring-only diff, no new branches) |
| `class_is_a_function` | `class_is_a_function`, `init_only_assigns` |
| `boolean_flag_param` | `boolean_flag_param` |
| `non_python_file` | none -- all labels false; also proves the `hunk_python` question ids never appear in the expected set for a non-Python hunk (a markdown file diff) |

Every case except `clean_change` and `non_python_file` also labels `<change>`'s
`new_behaviour_untested: true`: each adds new logic with no accompanying test, which a
human reviewer would flag regardless of the case's primary smell.

## A known gap

`MIN_POSITIVES` (5) means almost every id in this seed set reports `insufficient` --
each case contributes at most one or two positive hunks per id. `new_behaviour_untested`
is the one id with enough positives (6) to get a real best-threshold from the sweep.
Growing this set past the seed 8 (more shapes, more repeats of the same shape) is the
next step before trusting any threshold this prints; `docs/runs.md` records whichever
`questions.py` edits get made from that, per case.
