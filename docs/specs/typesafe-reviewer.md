# TypeSafe reviewer — spec (sketch)

**Status:** draft · **Replaces:** `assets/.claude/agents/reviewer.md` · **Pairs with:** `boundary-reviewer` (unchanged)

## 1. Goal

Rebuild the code reviewer as AI-powered software instead of an LLM agent. Code owns the
workflow: it runs the deterministic checks, slices the diff, asks Jev (TypeSafe's System
One model) narrow typed questions about each slice, and composes verdict, score and
findings in code. No model prose anywhere in the output.

Why: the current reviewer is a prompt. Its severities drift, it can invent findings to
avoid approving, and it "claims" checks it did not run. A System One reviewer cannot
claim anything: every finding is a question id with a probability behind it, every check
is a subprocess exit code, and the weights live in a Python file under version control.

## 2. Non-goals (v1)

- Prose `concrete_fix` written by a model. Each question carries a templated fix string.
- Governance rules and architectural seams. `boundary-reviewer` keeps those.
- Non-Python code. The rubric is the Python rubric from `reviewer.md`.
- Replacing the human. `NEEDS_HUMAN` stays a first-class verdict.

## 3. Contract (unchanged from `reviewer.md`)

- Runs in the builder's worktree, reviews `develop...HEAD`.
- Writes `ts-review.md` and `ts-review.json` at the worktree root, from scratch, same
  schema as the LLM reviewer's `review.md` / `review.json`. Distinct names so both
  reviewers can run on one worktree and their outputs sit side by side (§9).
- Verdicts `APPROVE` / `CHANGES_REQUESTED` / `NEEDS_HUMAN`; score 1–5; severities
  `blocker` / `important` / `minor` / `nit`; `stop_reason` as today.
- Never edits source.

Invocation replaces the `reviewer` subagent dispatch in the orchestrate skill:

```
uv run ts-review [--worktree <path>] [--task <task-file>] [--red-sha <sha>] [--base <ref>]
                 [--out <dir>]
                 [--range <A..B|A...B> | --commit <sha> | --ref <ref> | --pr <n>]
                 [--dump-state <dir>] [--record <dir>] [--replay <dir>]
uv run ts-review --calibrate <fixtures-dir>
```

Exit 0 on `APPROVE`, 2 on `CHANGES_REQUESTED`, 3 on `NEEDS_HUMAN`, 1 on tool failure.

### 3.1 CLI rules

- **Any worktree, any ref (RA-03).** `--worktree` is optional: it defaults to the
  toplevel of cwd (`git rev-parse --show-toplevel`), so `ts-review` run from inside
  any repo reviews that repo without a flag. `--task` and `--red-sha` are optional so
  the tool runs on an arbitrary folder; when they are absent the per-hunk review still
  runs in full, the task and red-proof checks are recorded as `not_run`, and the
  verdict is `NEEDS_HUMAN` / `missing_context` (§6.6). Findings are still written. The
  ad-hoc run is the experiment; the orchestrator always passes both flags.
- **Target modes.** At most one of `--range`, `--commit`, `--ref`, `--pr` (an argparse
  error otherwise). Each resolves to a `(base_sha, head_sha)` pair, both recorded in
  `ts-review.json` as full shas, alongside `range_source`
  (`worktree | range | commit | ref | pr`):
  - `--range A..B` / `A...B` → `merge-base(A, B)`, `B`.
  - `--commit S` → `S^`, `S`.
  - `--ref R` → `merge-base(<base>, R)`, `R`.
  - `--pr N` → an open PR: `merge-base(base_ref, head_sha)`, `head_sha`; a merged PR
    with a one-parent merge commit (squash): `M^`, `M`; a two-parent merge commit:
    `merge-base(M^1, M^2)`, `M^2`. `--pr` also implies `--task N` unless `--task` was
    given explicitly.
  - No mode flag → today's behaviour: `merge-base(<base>, HEAD)`, `HEAD` of
    `--worktree`.
  - An unknown ref exits 1; the pipeline never falls back to reviewing the wrong
    range.
  - In any ref mode the pipeline runs in a temporary detached worktree
    (`git worktree add --detach`), never the user's checkout, and the worktree is
    always removed afterwards, including when the pipeline raises.
- **`--base`** defaults to `develop`; if that ref does not exist, fall back to `main`,
  then `master`; if none exist, exit 1. Only used by the no-mode default and `--ref`.
  The chosen base is recorded in `ts-review.json`.
- **`--out <dir>`** is where `ts-review.md` / `ts-review.json` are written. Default: the
  worktree root in the no-mode case (today's contract), the source repo root (the
  resolved `--worktree`) in a ref mode.
- **Outputs.** Step 0 deletes `ts-review.md` and `ts-review.json` from `--out` (or its
  default) if they exist. Both are written last, atomically (temp file + rename), only
  after every step succeeded. A crash or API failure leaves neither file.
- **`--dump-state <dir>`** writes every state + question set as JSON and exits without
  calling the API. Used to inspect exactly what Jev sees and to seed fixtures. Only
  supports today's default target, not a ref mode.
- **`--record <dir>` / `--replay <dir>`** store and replay responses keyed by a hash of
  `(state, questions, model)`. The test suite runs on `--replay` only.
- **Task file** is the planner format (`tasks/README.md` in `new-project`): YAML
  frontmatter (`id`, `title`) plus an `## Acceptance` section whose bullets are the
  criteria. Parse bullets under that heading; each becomes `task.acceptance[n]`.
- **Progress** goes to stderr as plain lines (`checks…`, `hunk 3/14`, `compose`). No
  TUI in v1.

## 4. Pipeline

```
 0. clean       delete ts-review.md / .json if present       (code)
 1. checks      run red proof · green at HEAD · make check   (subprocess, no model)
 2. slice       git diff develop...HEAD → hunks + context     (code)
 3. gather      per hunk: task file, before/after excerpt,   (code)
                related tests, AGENTS.md shape section
 4. ask         one system_one() call per hunk,              (Jev, fan-out)
                every question in the catalog, in parallel
 5. change-wide one system_one() call over the whole change  (Jev)
                (task satisfaction, test adequacy)
 6. compose     thresholds + confidence gates → findings     (code)
                counts → verdict → score
 7. write       ts-review.md, ts-review.json                 (code)
```

Steps 4 and 5 use `AsyncTypeSafeClient` with a bounded semaphore. Every hunk gets every
question (speculative fan-out); code decides which answers matter for that hunk's
language and kind (new file, test file, deleted lines only, etc.).

### 4.0 SDK facts that shape the code (from the live docs, 2026-09-17)

- `client.system_one(state, questions, model=, retry=, timeout=)`. Questions are a
  `dict[str, Noul | Choice | Score]`; answers come back as `response.nouls`,
  `.scores`, `.choices`, each keyed by our id. `response.model` and
  `response.usage.input_tokens` feed `engine`.
- `NoulAnswer.noul` only. `ScoreAnswer.score` (expected value, 0..levels-1),
  `.probabilities` keyed by **int** level, `.confidence`. So `confidence` is `null`
  for every Noul finding and a number for every Score finding.
- `RetryPolicy()` default in SDK 0.6.0: `max_retries=2` on 408/429/5xx with backoff,
  30 s timeout. We set `RetryPolicy(max_retries=3)` and `timeout=60` explicitly; a
  35-question request over a large hunk may exceed the default.
- Errors are the `TypeSafeAPIError` family (`TypeSafeAuthenticationError`,
  `TypeSafeRateLimitError`, `TypeSafeAPITimeoutError`, …). Any of them surviving
  retries → exit 1, no outputs (§6.7).
- Structured `instructions` (`{question, inspect, focus}`) and Noul
  `criteria={true:…, false:…}` are documented and encouraged. The docs advise trying
  Noul questions with and without `criteria`; calibration (§9) tests both forms.
- Score level descriptions must describe concrete situations, not adjectives.
  `mixed_responsibility`, `scope_creep` and `new_behaviour_untested` levels are written
  as example code shapes, one sentence each.
- No documented cap on state size or question count. Measure; cap in code.

### 4.4 Slicing details

- Hunk boundaries from `git diff --unified=0 <base>...HEAD`; `hunk.after` from
  `git show HEAD:<path>` with 20 lines each side.
- Pass a temp attributes file (`*.py diff=python`) so hunk headers carry the enclosing
  `def`/`class`, which is `symbol_or_area`. Without it git only sees top-level names.
- Hunks over 400 lines are truncated to the first 400 and marked `truncated: true`
  in the state and in `ts-review.json` notes. Never silently.
- Language from extension. Non-Python hunks get §5.1 and §5.2 questions only.
- Test files: path under `tests/` or name matches `test_*.py` / `*_test.py`.

### 4.1 Deterministic checks

Same table as `reviewer.md`. Results are `pass | fail | not_run | not_applicable` with
the captured output tail as `notes`. Rules that need no model:

- No `--red-sha`, or the red commit passes the acceptance tests → `blocker`, finding
  `red_proof_missing`.
- `git diff <red-sha> HEAD -- <files>` non-empty, where `<files>` are the test files
  that exist at `red-sha` → `blocker`, finding `acceptance_tests_edited` → verdict
  `NEEDS_HUMAN` (a criterion the builder changed is a planning question). New test
  files added after the red commit never trigger this.
- `make check` non-zero → `blocker`, finding `gate_failed`, notes = first failing stage.

### 4.2 State shape (per hunk)

```json
{
  "task": { "id": "T-02", "title": "...", "acceptance": ["...", "..."] },
  "file": { "path": "src/pkg/config.py", "language": "python", "is_test": false, "is_new": false },
  "hunk": {
    "header": "@@ -40,12 +40,20 @@ def resolve_bind_address",
    "diff": "<unified diff text of this hunk>",
    "after": "<the same lines as they now read, with ~20 lines of context each side>"
  },
  "neighbours": {
    "same_module_helpers": ["<signatures of public functions in this module>"],
    "tests_touching_file": ["tests/test_config.py::test_bind_address_default"]
  },
  "conventions": "<AGENTS.md 'Architectural shape' + 'Always' + 'Never', verbatim>"
}
```

Questions point at paths with backticks (`` `hunk.diff` ``, `` `conventions` ``), per
the docs' structured-instructions guidance. Keep state small: one hunk, not the file.

### 4.3 State shape (change-wide)

```json
{
  "task": { "...": "as above, plus the full criteria text" },
  "diff_summary": [ { "path": "...", "added": 30, "removed": 4, "is_test": false } ],
  "src_diff": "<non-test diff, truncated to a size cap>",
  "test_diff": "<test diff since red-sha>",
  "acceptance_tests": "<the test functions committed at red-sha>"
}
```

## 5. Question catalog

Ids are stable; they double as finding ids. Every question has: type, instructions,
criteria, default severity, threshold, a `fix` template, and the rubric section it
encodes. Written in `review/questions.py` as `Noul`/`Score`/`Choice` objects with the
structured `{question, inspect, focus}` form. Sketch:

### 5.1 Failure direction (per hunk) — the ones that matter most

| id | type | instructions (short) | severity | threshold |
|---|---|---|---|---|
| `swallows_exception` | noul | Does `hunk.diff` add an `except` that continues without re-raise, specific handling, or a stated reason? | important | ≥ 0.7 |
| `bare_except` | noul | Does `hunk.diff` add a bare `except:` or `except Exception:` with no re-raise? | important | ≥ 0.7 |
| `success_on_unverified` | noul | Does `hunk.diff` add a path that returns success, `True`, or exit 0 on a condition it did not check? | blocker | ≥ 0.6 |
| `indistinguishable_default` | noul | Does `hunk.diff` add a default return that a caller cannot tell apart from a real value (empty string for a missing secret, `None` for "not found" and "error")? | blocker | ≥ 0.6 |
| `auth_passes_on_error` | noul | Does `hunk.diff` add an auth, credential, or permission check that passes when an exception is raised or a lookup fails? | blocker | ≥ 0.5 |
| `credential_in_output` | noul | Does `hunk.diff` put a token, key, or password into a log line, exception message, or `repr`? | blocker | ≥ 0.5 |

Blocker thresholds are low on purpose: fail closed. A false positive costs a fix round;
a false negative ends a project.

### 5.2 Correctness and fit (per hunk)

| id | type | instructions (short) | severity | threshold |
|---|---|---|---|---|
| `duplicates_helper` | noul | Does `hunk.diff` reimplement something in `neighbours.same_module_helpers`? | important | ≥ 0.7 |
| `violates_convention` | noul | Does `hunk.diff` do something `conventions` says Never, or skip something it says Always? | important | ≥ 0.7 |
| `magic_constant` | noul | Does `hunk.diff` add an unexplained literal that gates behaviour? | minor | ≥ 0.75 |
| `vague_error_message` | noul | Does `hunk.diff` raise or log an error that a reader could not act on? | minor | ≥ 0.75 |
| `mixed_responsibility` | score | How many distinct concerns (IO, parsing, logic, formatting) does the new code in `hunk.after` mix in one unit? levels: one / two related / three or more | important at ≥ 1.5 | conf ≥ 0.6 |

### 5.3 Python rubric (per hunk, only when `file.language == python` and not a test)

Each smell from `reviewer.md` becomes a Noul whose `criteria.false` carries the
"do NOT flag" exception, so the boundary is in the question, not in a post-filter.

| id | type | severity | false-criteria (what not to flag) |
|---|---|---|---|
| `class_is_a_function` | noul | minor | shared mutable state, invariants on construction, resource lifecycle, identity semantics |
| `init_only_assigns` | noul | minor | already a dataclass / NamedTuple |
| `all_static_class` | noul | minor | — |
| `speculative_polymorphism` | noul | important | two or more real implementations exist in the diff or the module |
| `self_as_config_bag` | noul | minor | — |
| `too_many_positional_params` | noul (≥5, esp. booleans) | minor | — |
| `boolean_flag_param` | noul | minor | flag mirrors an existing convention in `conventions` |
| `module_level_mutable_state` | noul | important | cache with invalidation, registry built at import |
| `temporal_coupling_unenforced` | noul | important | — |
| `kwargs_passthrough_undocumented` | noul | minor | — |
| `missing_type_hints_public` | noul | important | untouched pre-existing code |
| `missing_type_hints_private` | noul | minor | untouched pre-existing code |
| `abc_over_protocol` / `os_path_over_pathlib` / `format_over_fstring` / `constants_over_enum` | noul | nit | local convention already uses the alternative |

### 5.4 Task satisfaction (change-wide)

One Noul **per acceptance criterion**, built in a loop from the task file:

- `criterion_{n}_satisfied`: Does `src_diff` implement `task.acceptance[n]`? — a `no`
  (noul ≤ 0.4) is a `blocker`; 0.4–0.7 is `NEEDS_HUMAN` (cannot tell).
- `criterion_{n}_tested`: Does `acceptance_tests` exercise `task.acceptance[n]`?
- `tests_fitted_to_code`: Does `test_diff` weaken, remove, or special-case assertions
  that existed at the red commit? blocker ≥ 0.6.
- `scope_creep`: Score, 3 levels, over `diff_summary` + `task`: one change / one change
  plus a related tweak / several independent changes. important at ≥ 1.5.

### 5.5 Test adequacy (change-wide and per test hunk)

| id | type | severity | note |
|---|---|---|---|
| `new_behaviour_untested` | score 0–3 (none / some / most / all new paths covered) | important ≤ 1; blocker ≤ 1 when `touches_high_risk` fires. **Fires low**: the only question with `direction: le` | change-wide |
| `touches_high_risk` | noul: credentials, auth, disk writes outside the repo | modifier only | change-wide |
| `patches_unit_under_test` | noul | important | per test hunk |
| `mock_hides_integration` | noul | minor | per test hunk |
| `unittest_testcase_style` | noul | nit | per test hunk |

## 6. Composition rules

All in `review/compose.py`; nothing here touches the model.

1. **Threshold, then gate.** A Noul fires when `noul ≥ threshold`. A Score fires when
   `score` crosses its level **and** `confidence ≥ 0.6`; below that it is not a finding
   but is listed under "Uncertain" in `ts-review.md` for the orchestrator's eye.
2. **Severity is a table lookup** on question id, with modifiers (`touches_high_risk`
   promotes test findings to blocker). No model chooses severity.
3. **Dedupe** the same id across adjacent hunks of one symbol into one finding.
4. **Verdict:** `acceptance_tests_edited`, any `criterion_*_satisfied` in the grey
   zone, or missing context → `NEEDS_HUMAN`; else any deterministic-check `blocker`,
   any model `blocker`, or any `important` → `CHANGES_REQUESTED`; else `APPROVE`.
   `NEEDS_HUMAN` takes precedence: a human question outranks a fix round.
5. **Score** is derived, never chosen: 1 if any `success_on_unverified`/`auth_passes_on_error`
   or a criterion unsatisfied; 2 if any blocker or failed check; 3 if any important;
   4 if only minor/nit; 5 if none. Same hard constraints as `reviewer.md`.
6. **Missing context** (no task file, no red sha, diff empty) → `stop_reason:
   missing_context`, verdict `NEEDS_HUMAN`. Never guess.
7. **API failure** after retries → exit 1, no `ts-review.json`. The orchestrator must not
   read a stale file: the CLI deletes both outputs before it starts.

## 7. Output

`ts-review.json` has the `review.json` schema plus two fields per finding:

```json
{ "question_id": "swallows_exception", "probability": 0.91, "confidence": null }
```

and a top-level `"engine": {"model": "jev-latest", "requests": 14, "input_tokens": 41200}`.

`concrete_fix` comes from the question's `fix` template with `{symbol}` and `{path}`
filled. `symbol_or_area` is the hunk header's function name or `<module>`.

`ts-review.md` is `review.md` plus an **Uncertain** section: fired-but-low-confidence scores and any
`criterion_*` in the grey zone, each with its probability. The orchestrator reads it;
the builder does not.

## 8. Layout

Two phases. Nothing in `assets/` or the current build loop changes until phase 1 works.

### Phase 1 — experiment, this repo (`typesafeai-review`)

This package is its own repo now, so the layout sits at the root, not under `tools/`.

```
pyproject.toml      package `typesafe-review`; deps typesafe-sdk, pyyaml; script `ts-review`
src/typesafe_review/
  cli.py            argparse entry, flag rules (§3.1)
  verdict.py        Verdict enum + exit codes (imported by cli and compose)
  checks.py         subprocess runners for the deterministic table
  slicing.py        git diff → hunks, context, neighbours (§4.4)
  taskfile.py       planner task file → task dict (frontmatter + Acceptance bullets)
  state.py          TypedDicts for the two state shapes
  questions.py      the catalog: dataclass Question(id, primitive, severity, threshold, fix, scope)
  ask.py            async fan-out with semaphore + RetryPolicy; record/replay
  compose.py        §6
  render.py         ts-review.md / ts-review.json, atomic write
tests/              replayed responses as fixtures, no live calls
fixtures/           labelled diffs for §9
```

Build order, each step runnable on its own:

1. `cli` + `checks` + `slicing` + `taskfile` + `--dump-state`. No API. Verified by
   dumping state for a real diff and reading it.
2. `questions` + `ask` + `compose` + `render`, with `--record` / `--replay`. Tests on
   replayed responses.
3. First real runs via `--worktree` on the user's other repos. Log `engine.input_tokens`
   and wall time per review. Compare `ts-review.json` with the LLM reviewer's `review.json` on the
   same diff.
4. Calibration fixtures and `--calibrate` (§9).

TUI (`ink`, `rich`, `textual`) is out of scope for v1. The tool is a Python package
consumed by an orchestrator; a Node TUI would mean a second runtime for a progress bar.

### Phase 2 — ship as a dependency, not vendored

The reviewer is tooling, like ruff or ty. It should not land in the target codebase's
`src/`, and Python under `.claude/` fights ruff, ty and `pythonpath`. So:

- Publish `typesafe-review` (own repo or a git URL). Scaffolded projects add it to the
  `dev` dependency group; `uv run ts-review` resolves to it.
- `assets/` changes then are small: one dep line, `TYPESAFE_API_KEY=` in `.env.example`,
  the orchestrate skill's reviewer dispatch → `uv run ts-review ...`, `reviewer.md` deleted.
- Upgrades reach every project without re-scaffolding.

## 9. Calibration

Thresholds above are guesses. Before trusting them:

1. Build a fixture set from this repo's own history: ~20 diffs, each hand-labelled with
   the findings a human reviewer would raise. Include known bad shapes (swallowed
   except, empty-secret default, edited acceptance test).
2. `uv run ts-review --calibrate fixtures/` prints per-question precision/recall at the
   current thresholds and the threshold that maximises F0.5 (precision-weighted) for
   minors, F2 (recall-weighted) for blockers.
3. Thresholds are constants in `questions.py`, reviewed like any code. Record each
   change in `docs/runs.md` (run metrics and threshold history; `docs/ledger-findings.md`
   is the governance triage ledger and stays orchestrator-only).
4. Re-run calibration when `TYPESAFE_DEFAULT_MODEL` changes. Pin the model id in
   `.env.example`, not `jev-latest`, once a version is calibrated.

## 10. Open questions

- **State size.** The docs give no documented cap on state length. Assume hunks fit;
  cap `src_diff` in the change-wide state at N KB and record truncation in `notes`.
  Measure `usage.input_tokens` in the first runs.
- **Question count per request.** Docs say "many" with no limit. ~35 per hunk is the
  plan; test it.
- **Cost per review.** Unknown until priced. Track `engine.input_tokens` in `docs/runs.md`.
- **Who writes the fix prose when a template is not enough.** Out of scope for v1; the
  orchestrator already forms its own view before forwarding.

## 11. Tasks

Files in `tasks/typesafe-reviewer/` (untracked, see `tasks/README.md`). Run with `/orchestrate tasks/typesafe-reviewer`.

| id | title | depends_on |
|---|---|---|
| T-01 | Package scaffold, `review` CLI entry, output hygiene | — |
| T-02 | Parse planner task files | — |
| T-03 | Slice `base...HEAD` into hunks | — |
| T-04 | Deterministic checks | — |
| T-05 | State builders, `--dump-state` | T-01, T-02, T-03 |
| T-06 | Question catalog | — |
| T-07 | Async fan-out, record/replay | T-06 |
| T-08 | Compose findings, verdict, score | T-06 |
| T-09 | Render outputs atomically | T-08 |
| T-10 | Pipeline end to end, first real run | T-04, T-05, T-07, T-09 |
| T-11 | Calibration | T-10 |

Parallel batches: {T-01, T-02, T-03, T-04, T-06} → {T-05, T-07, T-08} → T-09 → T-10 → T-11.
`files` overlap on `cli.py` (T-01, T-05, T-10, T-11) forces those four to run in order.
