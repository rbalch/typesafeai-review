# Ledger findings

The experiment log. Every dislike of agent output gets sorted into a bin and recorded
here, whether or not it becomes a control. An unlogged finding is a lost data point.

- **Bin 1** — already covered by a linter or type-checker. The harness adds nothing.
- **Bin 2** — a concrete, checkable, systemic pattern. **This is the value.**
- **Bin 3** — genuine subjective taste. No control will ever catch it.

Rule of three: a Bin 2 finding stays in the soft layer (a note, a nudge, a one-off
correction) until its **third** sighting. Only then does it earn a decision and a
CI-enforced control.

**The falsifiable test:** if Bin 2 stays fat and review burden measurably shrinks, the
harness earns its keep. If nearly everything lands in Bin 1 or Bin 3, this is
complicated linters plus a wiki and we should say so and drop it. Do not judge it by
"does CI go red."

See the `finding-triage` skill for the procedure and the entry template.

Findings about the **harness itself** — something it got wrong, friction that felt like
ceremony, a control that fired on correct code — are worth logging here too. Mark them
as harness findings and leave them unbinned; the bins sort dislikes of *agent output*,
and forcing a harness observation into one loses what makes it interesting.

---

## Findings

### F-1 — Input validator accepts a superset of what the code then acts on
- **Date:** 2026-09-17 (T-01)
- **Bin:** 2
- **Claim:** A path check uses `git rev-parse --git-dir` (true anywhere inside a repo) while the code that follows assumes the repo root; a subdirectory passed as `--worktree` had `sub/review.md` deleted. Checkable: any `rev-parse --git-dir` guard not paired with a `--show-toplevel` equality.
- **Sightings:** 3 (T-01 `--git-dir` guard; T-03 `_show_file_at_head` classified "path missing at HEAD" by matching git's stderr text, which also matched a corrupted path, so an existing file got an empty context window; T-04 red proof counted *any* non-zero pytest exit as "tests failed", so a red commit with zero tests (exit 5) passed the red proof, and a git failure inside the edited-tests check was swallowed into "nothing edited")
- **Action:** **third sighting reached on 2026-09-17. Ready for `control-author`.** Candidate claim for the control: a guard whose success branch is reached by a condition broader than the one the following code relies on (`rev-parse --git-dir` for "is root", `returncode != 0` for "tests failed", `except X: return default`). Whether a fitness control can check this mechanically is the control-author's call; it may refuse. Not dispatched yet: the orchestrator was told to stop after T-04.
- **Notes:** Found by the orchestrator, not either reviewer. Both reviewers scored 5/5 with zero findings; the fail-closed reading of "root only" was in the task text and still slipped past both.

### F-2 — Task ids hardcoded as strings in code
- **Date:** 2026-09-17 (T-01)
- **Bin:** 3
- **Claim:** `cli.py` maps unimplemented flags to task ids (`T-02`, `T-04`, ...) as literals; renumbering the plan drifts silently.
- **Sightings:** 1
- **Action:** none — taste; the strings are user-facing messages and will be deleted as tasks land
- **Notes:** Raised by boundary-reviewer as Bin 3; agreed.

### F-3 — I/O exception leaks past a module's declared error type
- **Date:** 2026-09-17 (T-02)
- **Bin:** 2
- **Claim:** `taskfile.py` declared `TaskFileError` as its one failure type, but `path.read_text()` let `FileNotFoundError` / `PermissionError` escape. Checkable: a module that defines `<X>Error` and calls `read_text`/`open`/`subprocess.run` outside a `try` that re-raises as `<X>Error`.
- **Sightings:** 2 (T-02 `taskfile.py`; T-05 `cli.py` `_dump_state` left `mkdir`/`write_text` unwrapped while every other call in the function was, so a dump dir that is a file gave a raw `FileExistsError` traceback instead of exit 1 with a message. `state.py` itself was clean: every read and subprocess wrapped as `StateError`, though the wrapping branches had no tests until review.)
- **Action:** soft — both fixed. One more graduates it. Candidate control: a function that wraps some `open`/`write_text`/`mkdir`/`subprocess.run` calls in `try` but not others.
- **Notes:** Orchestrator and code reviewer found it independently; boundary reviewer did not. Reviewer proposed Bin 3; binned as 2 because a grep can find it.

### F-4 — Regex `match=` given an unescaped path string
- **Date:** 2026-09-17 (T-02)
- **Bin:** 3
- **Claim:** `pytest.raises(..., match=str(p))` treats a filesystem path as a regex. Harmless with tmp_path today.
- **Sightings:** 1
- **Action:** fixed by the orchestrator in the squash (`re.escape`); no ruff rule covers it, not worth one

### F-5 — Subprocess text used as a path without normalising the tool's quoting
- **Date:** 2026-09-17 (T-03)
- **Bin:** 2
- **Claim:** `+++ b/<path>` from `git diff` carries a trailing tab when the path has a space; the parser used it verbatim, so `Hunk.path` was `"my file.py\t"`. Checkable: any parse of `---`/`+++`/`diff --git` lines that does not strip the tab or set `core.quotePath=false`.
- **Sightings:** 2 (T-03 `+++` path; T-04 `git diff --name-only` parsed with `splitlines()` and no `-z`, caught in review before merge)
- **Action:** soft — both fixed; `state.py` (T-05) is the next watch point. One more graduates it.

### F-6 — `subprocess.run(text=True)` on tool output silently drops `\r`
- **Date:** 2026-09-17 (T-03)
- **Bin:** 2
- **Claim:** universal-newline decoding stripped CR from captured diff and file content, so CRLF files were misrepresented. Checkable: `subprocess.run(... text=True)` (or `universal_newlines=True`) where the stdout is data, not a message.
- **Sightings:** 1
- **Action:** soft — T-03 captures bytes and decodes; T-04 builder brief warns about it

### F-7 — File-level facts duplicated onto every hunk
- **Date:** 2026-09-17 (T-03)
- **Bin:** 3
- **Claim:** `language` and `is_test` sit on each `Hunk` and on `FileSummary`. The spec's per-hunk state shape wants them per hunk anyway; accepted by design.
- **Sightings:** 1
- **Action:** none

### F-8 — Redaction regex is the task's literal pattern and misses `Authorization: Bearer`
- **Date:** 2026-09-17 (T-04)
- **Bin:** 3 (spec gap, not a code defect)
- **Claim:** `(?i)(token|secret|key)\s*[=:]\s*\S+` is what T-04 asked for; bearer headers and `password=` pass through into notes.
- **Sightings:** 1
- **Action:** none in T-04; widen the pattern in the spec before T-10's first real run on a target repo

### H-2 — Harness: a reviewer finding built on an unrepresentative fixture
- **Date:** 2026-09-17 (T-04)
- **Bin:** harness, unbinned
- **Claim:** boundary-reviewer rated "`uv run pytest` writes caches into the reviewed worktree" HIGH after running against a bare toy repo with no `.gitignore` or lockfile. Against a real target the tracked tree is untouched. The orchestrator disagreed with evidence and did not forward it; the reviewer re-tested and accepted.
- **Notes:** The "reviewer wins unless you can personally verify" rule worked as intended. Reviewer briefs could say: reproduce on a fixture shaped like a real target.

### H-1 — Harness: planner task files restated spec and repo facts, and drifted
- **Date:** 2026-09-17 (pre-build critic pass)
- **Bin:** harness, unbinned
- **Claim:** The 11 task files carried facts that contradicted the spec or the tree: T-01 said the repo had no governance harness and told the builder to rewrite `Makefile`; T-10 told the builder to *create* this ledger file; T-04 and T-06 both owned severity for the same finding ids; T-06/T-08 assumed one Score direction while spec §5.5 fires low; T-11 could not run because T-01 required `--worktree`. Eight task-critic runs plus an orchestrator read caught 6 blockers and ~20 nits before any code.
- **Sightings:** 1 (one plan)
- **Action:** all fixed in the task files and spec (commit a07aee3); `docs/runs.md` created as the home for run metrics so this file stays orchestrator-only
- **Notes:** The critic pass earned its keep on the first plan. One critic finding was false (claimed `SystemOneResponse` lacks `.nouls/.scores/.choices`; they are properties, so struct-field introspection missed them). Verify SDK claims by calling, not by listing fields.

### F-9 — Read-only mode shares an entry path with a mutating step
- **Date:** 2026-09-17 (T-05)
- **Bin:** 2
- **Claim:** `cli.py` `main()` ran `_clean_stale_outputs(worktree)` (deletes `review.md`/`review.json` in the target) before branching on `--dump-state`, so an inspection mode documented as "nothing written in the worktree" deleted files in it. Checkable: a side-effecting call that precedes the branch selecting a mode declared read-only; or, per mode, a test that plants the artefacts the mutating step touches and asserts they survive.
- **Sightings:** 1
- **Action:** soft — fixed; the acceptance test now plants stale outputs before `--dump-state`. Watch `--record`/`--replay` (T-07) and `--calibrate` (T-11), both of which add modes to the same `main()`.
- **Notes:** Found by boundary-reviewer by execution against a fixture with planted files; the code reviewer rated it minor as "inherited T-01 behaviour". The orchestrator had flagged it from reading the diff before either report. The green acceptance test did not seed the files it claimed to protect (see F-10).

### F-10 — Tests assert a data table's shape, not its values
- **Date:** 2026-09-17 (T-06)
- **Bin:** 2
- **Claim:** `tests/test_questions.py` checked id presence, uniqueness, level counts and direction, but no test pinned any severity or threshold, and none checked that §5.3 Nouls carried `criteria` at all: flipping `success_on_unverified` from blocker to minor and deleting a question's criteria both stayed green. Same shape in T-05: the "nothing written in the worktree" test never planted anything to be deleted. Checkable by mutation: a catalog/table module whose tests survive changing a value. Mechanically: a test module over a module of literals with no assertion comparing a literal to an expected literal.
- **Sightings:** 1 (T-06 severity/threshold/criteria; T-05 planted-file gap counted with it as one batch)
- **Action:** soft — fixed with a spec-transcribed pin table and a criteria test; the reviewer's mutation pass (10 plants) is what found it. Reviewer briefs should keep the mutation list.

### H-3 — Harness: red proof that only proves the module is missing
- **Date:** 2026-09-17 (T-05, T-06)
- **Bin:** harness, unbinned
- **Claim:** Both red commits failed at collection with `ModuleNotFoundError` — true red, but it proves nothing about any assertion. Both builders then edited the test files after the red commit (T-05: fixture bugs found while implementing; T-06: `isinstance` narrowing for `ty`), and the T-06 builder disclosed it had written the implementation first and staged the red commit after. The orchestrator diffed both edits: additions and equivalent narrowings only, nothing loosened, so neither was `tests_fitted_to_code`. But the process check `acceptance_tests_edited` (spec §4.1) would have fired on both, correctly, and the reviewer had no way to tell fitted from fixed without reading every line.
- **Sightings:** 2 (one batch)
- **Notes:** This is exactly the case the tool under construction must handle. Two implications for the spec: (1) `acceptance_tests_edited` should carry the diff of `tests/` since red so the human can judge in seconds, not fire as a bare blocker; (2) a red proof for a new module could require the builder to stub the module (`raise NotImplementedError`) so the tests fail on assertions, not imports. Neither is decided; both are planning questions for T-08/T-10.

### H-1 — sighting 2 (2026-09-17, T-05/T-06 critic pass)
- Task-critic found: T-06 stated "every question is `ge` except one" while §5.4 has two more `le` questions; T-05 never said to remove the stale `_NOT_IMPLEMENTED` gate that would have blocked its own `--task`/`--red-sha` paths; `diff_summary` would have leaked a `binary` key the spec lacks; `criterion_{n}_tested` had no severity anywhere. One human decision (severity for `_tested`), rest applied as nits. Task-file drift is now at two sightings across two batches.

<!--
### F-1 — <one-line description>
- **Date:** YYYY-MM-DD
- **Bin:** 2
- **Claim:** <the dislike, stated so a machine could check it>
- **Sightings:** 1
- **Action:** soft — noted, no control yet
- **Notes:** <anything surprising about the harness itself>
-->
