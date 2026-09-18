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
- **Action:** **dispatched to `control-author` on 2026-09-17; partially controlled.** The general claim ("a guard broader than what follows relies on") was refused as a single control: `checks.py` today has a *correct* `returncode != 0` after invoking pytest (`_green_at_head`, where exit 5 must also fail) sitting next to a *correct* narrowed `except OSError` around a subprocess call (`_run`) that returns a default on `TimeoutExpired` deliberately — a mechanical rule for either shape fires on that code as it stands. DEC-1 / `controls/fitness/git_dir_root_guard.py` covers only the first sighting: `rev-parse --git-dir` without a paired `rev-parse --show-toplevel` in the same file, under `src/`. The pytest-exit-code and bare-`except` sightings remain un-controlled; watch for a fourth sighting narrow enough to be a pure syntax tell.
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
- **Sightings:** 4 (T-02 `taskfile.py`; T-05 `cli.py` `_dump_state` left `mkdir`/`write_text` unwrapped while every other call in the function was, so a dump dir that is a file gave a raw `FileExistsError` traceback instead of exit 1 with a message. `state.py` itself was clean: every read and subprocess wrapped as `StateError`, though the wrapping branches had no tests until review. T-07 `ask.py` declared `AskFailed` as the one failure type, then `Record.save` (`mkdir`/`write_bytes`) and `Replay.load` (caught `FileNotFoundError` only, so `IsADirectoryError`/`PermissionError` escaped) leaked raw `OSError`; a corrupt cached body would have escaped too had the SDK not wrapped it. T-09 `render.py` `write_outputs`: the rollback loop's `Path.unlink` calls sat outside any `try` while every write and `os.replace` was wrapped; a failed unlink would have escaped as raw `OSError` **and** skipped the remaining files, breaking the "neither file exists after a failure" invariant. DEC-1 passed green: `unlink` and `os.replace` are not in the control's method list. Both reviewers found it; the control did not.)
- **Action:** **graduated 2026-09-17: DEC-1 `controls/fitness/io_error_wrapping.py`, PR #9.** Subset: free functions only, any `try` counts; it catches the T-02 shape, not T-07's recorder methods. Accepted as a narrow true rule over a wide false one. **T-09 shows the list is one method short**: cleanup paths use `unlink` / `os.replace`. One sighting of the gap; a second and DEC-1 gets superseded with the two methods added. Candidate control: a module that defines `<X>Error` (or `<X>Failed`) and calls `open`/`read_text`/`read_bytes`/`write_text`/`write_bytes`/`mkdir`/`subprocess.run` outside a `try` whose handler raises `<X>Error`.
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
- **Sightings:** 4 (T-06 severity/threshold/criteria; T-05 planted-file gap counted with it as one batch. T-07: `AskResult.model` sourced from the `model` argument instead of the response survived the whole suite because every fixture and test used the same model string for both. T-08: 8 of 16 mutants survived the first suite; then the fix-round test factory recomputed the "expected question ids" the same way `compose.py` does, so dropping `is_test`/`language`/`criterion_questions` from the gate stayed green. Fixed with a spec-transcribed literal id table, the same cure as T-06. T-11: `f_beta` and `sweep_best_threshold` were each tested alone with literal betas; nothing asserted which beta `build_row` chose or that the row's best threshold came from the sweep. Swapping F0.5/F2 and replacing the sweep with the catalog threshold both survived 10 tests. Fixed with hand-computed literals through `build_row`.)
- **Action:** **third sighting reached on 2026-09-17 (T-08). Candidate for `control-author`, not yet dispatched** — the orchestrator wants a human's view first: the working detector is the reviewer's mutation pass, and the two cures (spec-transcribed pin tables; a helper that reuses the code under test) point at a test-shape rule, not an `src/` rule. A control would have to find a test module that imports a helper from the module it tests and uses it to build expectations.

### H-3 — Harness: red proof that only proves the module is missing
- **Date:** 2026-09-17 (T-05, T-06)
- **Bin:** harness, unbinned
- **Claim:** Both red commits failed at collection with `ModuleNotFoundError` — true red, but it proves nothing about any assertion. Both builders then edited the test files after the red commit (T-05: fixture bugs found while implementing; T-06: `isinstance` narrowing for `ty`), and the T-06 builder disclosed it had written the implementation first and staged the red commit after. The orchestrator diffed both edits: additions and equivalent narrowings only, nothing loosened, so neither was `tests_fitted_to_code`. But the process check `acceptance_tests_edited` (spec §4.1) would have fired on both, correctly, and the reviewer had no way to tell fitted from fixed without reading every line.
- **Sightings:** 2 (one batch)
- **Notes:** This is exactly the case the tool under construction must handle. Two implications for the spec: (1) `acceptance_tests_edited` should carry the diff of `tests/` since red so the human can judge in seconds, not fire as a bare blocker; (2) a red proof for a new module could require the builder to stub the module (`raise NotImplementedError`) so the tests fail on assertions, not imports. Neither is decided; both are planning questions for T-08/T-10.

### H-1 — sighting 2 (2026-09-17, T-05/T-06 critic pass)
- Task-critic found: T-06 stated "every question is `ge` except one" while §5.4 has two more `le` questions; T-05 never said to remove the stale `_NOT_IMPLEMENTED` gate that would have blocked its own `--task`/`--red-sha` paths; `diff_summary` would have leaked a `binary` key the spec lacks; `criterion_{n}_tested` had no severity anywhere. One human decision (severity for `_tested`), rest applied as nits. Task-file drift is now at two sightings across two batches.

### F-11 — `except` catches a library subclass, not the library's base error
- **Date:** 2026-09-17 (T-07)
- **Bin:** 2
- **Claim:** `ask.py` caught `TypeSafeAPIError` after retries; the SDK's `TypeSafeAPIConnectionError`/`TypeSafeAPITimeoutError` derive from `TypeSafeError` directly, so a plain network failure escaped `ask_all` unwrapped, breaking the module's own "everything becomes `AskFailed`" contract. Checkable: an `except <lib>.<Sub>Error` where `<lib>` exports sibling error classes not derived from `<Sub>Error`, and no sibling is caught elsewhere in the same `try`.
- **Sightings:** 1
- **Action:** soft — fixed (`except TypeSafeError`) with a `ConnectError` transport test. Watch T-10, which wires the CLI's exit-1 mapping over the same family.
- **Notes:** Blocker found by the code reviewer by execution; the boundary reviewer scored 5/5 twice without it. The builder had read the docs' error list (§4.0 names both siblings) and still picked the narrower class.

### F-12 — Fan-out without cancellation leaves siblings running after the first failure
- **Date:** 2026-09-17 (T-07)
- **Bin:** 2
- **Claim:** `asyncio.gather(*coros)` re-raised the first exception while the other requests kept retrying, sleeping and writing to the record dir. Checkable: `asyncio.gather` over network calls without `TaskGroup`, `return_exceptions=True` plus cancel, or `wait(FIRST_EXCEPTION)` plus cancel.
- **Sightings:** 1
- **Action:** soft — fixed with `wait(FIRST_EXCEPTION)`, cancel-and-await siblings, `aclose()` in `finally`; test asserts the slow sibling is cancelled.

### F-13 — No test asserts that a log or error surface omits the secret or the state
- **Date:** 2026-09-17 (T-07)
- **Bin:** 2
- **Claim:** the stderr log line and `AskFailed`/`ReplayMiss` messages were verified secret-free only by the boundary reviewer's canary run; nothing in the suite pins it, so a later edit that adds `state` to the message stays green. Checkable: a module that writes to stderr or formats an exception message near a credential-bearing client, with no test grepping that output for a planted canary.
- **Sightings:** 1
- **Action:** soft — noted; a canary test is cheap and T-10 (the CLI, which prints and exits) is the place to add it.
- **Notes:** Raised by boundary-reviewer as a candidate; agreed.

### F-14 — Defensive dead code: an `except` arm or cleanup branch no input can reach
- **Date:** 2026-09-17 (T-07)
- **Bin:** 3
- **Claim:** a `msgspec.DecodeError` arm the SDK already pre-empts, and an inner cancel-and-gather block shadowed by the outer `except BaseException` handler that always ran. Neither was a bug; both were "defence in depth" a reader has to disprove. Taste; removed in the squash.
- **Sightings:** 1
- **Action:** none

### H-1 — sighting 3 (2026-09-17, T-07/T-08 critic pass)
- T-07 fixed `ask_all`'s signature with no seam for the fake transport three acceptance bullets required. T-08 said `le` was unique to `new_behaviour_untested` (the catalog has two more), named an answer shape no module defines and omitted T-07 from `depends_on`, left the criterion grey zone's 0.7 with no home, dropped the spec's "adjacent" from dedupe, and used template placeholder names for dataclass fields. One human decision (T-08 now depends on T-07, stacked on its branch), five nits applied. Three batches, three sightings: the planner writes signatures and shapes before the code they must fit exists. Harness, unbinned; the fix is in the planner skill, not a control.

### H-3 — sighting 3 (2026-09-17, T-07)
- The builder brief asked for a `NotImplementedError` stub so the red proof fails on assertions, not imports. It did: 10 tests red on the stub. Post-red test edits were a ruff reformat, one unused import, and an autouse env fixture the SDK's client constructor forced (it refuses to build without `TYPESAFE_API_KEY` even with a fake transport). Additions only; the orchestrator diffed the formatted red file against HEAD. The stub approach answers implication (2) from the first H-3 entry; keep it in every builder brief.

### F-15 — Answers consumed by presence, not by what was asked: absence reads as "did not fire"
- **Date:** 2026-09-17 (T-08)
- **Bin:** 2
- **Claim:** `compose.py` iterated the ids present in the response; a response with no answers at all, in full context, composed to APPROVE 5/5. `Choice` answers were never read. Checkable: a consumer of a keyed response that iterates `response.items()` without diffing against the set of keys it requested. Same family as F-1 (a success branch reached by a condition broader than what the code relies on), logged separately because the shape is specific: request set vs. response set.
- **Sightings:** 1
- **Action:** soft — fixed: compose derives the expected set from the catalog and any gap forces `NEEDS_HUMAN` with `stop_reason: unanswered_questions`; a `Choice` in the catalog raises. Spec §6.6 did not cover a partial response; the PR asks the human to accept the addition. Watch T-10, which is the first place the request and response sets are built by two different modules.
- **Notes:** The orchestrator flagged it from the builder's own "Choice silently ignored" disclosure before either report; both reviewers then confirmed by execution. First T-08 review scores: boundary 2/5, code 3/5.

### F-16 — A sibling module's private helper copied instead of exposed
- **Date:** 2026-09-17 (T-08)
- **Bin:** 2
- **Claim:** `compose.py` re-declared `slicing.py`'s three private regexes to get a symbol from a hunk header, because `HunkState` (per spec §4.2) carries no symbol. Checkable: a regex or constant literal duplicated byte-for-byte across two modules under `src/`. Fixed by giving `slicing.py` a public `symbol_from_header()`; one file outside the task's footprint.
- **Sightings:** 2 (T-08; T-11: `_expected_hunk_questions`/`_expected_change_questions` existed in `compose.py`, `pipeline.py` and `calibrate.py`, each wrapping `questions_for`/`criterion_questions` the same way, plus `_git`/`_rev_parse` copied from the sample repo builder into `testrepo.py`. The boundary reviewer called the triple "sanctioned precedent from F-15"; it is not, F-15 asked for one source and got three wrappers of it. Fixed: two public functions in `questions.py`, all three call them; `build_repo.py` imports `testrepo`'s git helpers.)
- **Action:** soft — fixed. The builder claimed `state.py` "sets precedent" for the copy; the boundary reviewer checked and it does not.

### F-17 — Fix-round test edits loosen assertions the fix did not require
- **Date:** 2026-09-17 (T-08)
- **Bin:** 2
- **Claim:** four `findings == []` / exact-list assertions became "no finding with this id" during a factory rewrite; the neutral backdrop still composed to APPROVE with no findings, so the strict forms passed. "Nothing else fired" silently stopped being checked. Checkable per test file: an assertion of `== []` or an exact list at the red SHA that becomes an `all(...)`/`any(...)`/filtered form at HEAD. This is exactly what `tests_fitted_to_code` (spec §5.4) asks Jev; the deterministic version is a diff shape.
- **Sightings:** 1
- **Action:** soft — reverted. The orchestrator spotted the shape in the assertion-line diff; the code reviewer proved the strict forms still passed. Candidate deterministic check for `checks.py` in T-10: report weakened assertion shapes in `tests/` since red, so `acceptance_tests_edited` carries evidence (see H-3).

### H-3 — sighting 4 (2026-09-17, T-08)
- Post-red edits again, and this time large: a 389-line factory rewrite forced by a real fix (the unanswered gate made sparse inputs NEEDS_HUMAN). Additions and tightening, plus the four loosenings in F-17. The orchestrator's assertion-line grep (`git diff <red> HEAD -- tests/ | grep -E '^[-+].*assert'`) was enough to find them in one screen. Worth making that grep a deterministic check.

### F-18 — Test fixture values outside the domain the producer can emit
- **Date:** 2026-09-17 (T-09)
- **Bin:** 3
- **Claim:** a NEEDS_HUMAN golden carried `probability=1.6`; `compose.py` can only emit [0, 1]. Render does not validate, correctly, so nothing failed; the example misleads a reader. A machine could check a fixture against a schema, but no schema exists for the renderer's input and writing one for this is ceremony. Taste; fixed in the fix round.
- **Sightings:** 1
- **Action:** none

### F-19 — Defensive helper shipped without a test that reaches it
- **Date:** 2026-09-17 (T-09)
- **Bin:** 1
- **Claim:** `_table_cell` (pipe/newline escaping for the Required Checks table) existed for raw subprocess notes but no fixture carried a `|` or `\n`, so a no-op mutation survived. Coverage tooling would show the branch as executed but the mutation-survival needs a mutation run; the builder disclosed it. Related to F-3's note "wrapping branches had no tests until review" and F-14. Fixed in the fix round with a literal expected row.
- **Sightings:** 1
- **Action:** none; if it recurs, add `--cov` with a per-file floor to `make test` rather than a control.

### H-1 — sighting 4 (2026-09-17, T-09 critic pass)
- T-09 gave `render_markdown` an `engine` argument nothing in the spec or template consumes, and said `file:symbol` with no rule for `Finding.file = None`, which `compose.py` emits for every change-wide finding. Two nits, both applied by the orchestrator before the builder. Same cause as the three prior sightings: signatures written before the code they must fit. The renamed outputs (`ts-review.*`, Ryan's call) were a planning gap too: spec §9 compares two reviewers' JSON on one worktree and gave them the same file name.

### H-3 — sighting 5 (2026-09-17, T-09)
- The red proof for two test files was one commit; `test_render.py` failed at collection (`ModuleNotFoundError`) and pytest never reached `test_cli.py`'s renamed assertion, so one red run proved one file. The code reviewer ran `test_cli.py` alone at the red SHA and confirmed it was red for the right reason. Keep the `NotImplementedError` stub instruction in builder briefs (it was omitted this time) so the red run reaches assertions, and when a task touches two test files, run each alone at the red SHA.

### F-20 — A measured column in docs/runs.md filled with a neighbouring metric
- **Date:** 2026-09-17 (T-10)
- **Bin:** 3
- **Claim:** both rows said `hunks: 3`; `slice_diff` returns 2, the third fixture file is the change-wide request. Spec §10 says measure, never fabricate, and the number was read off the fixture directory instead of the slicer. A test could assert `runs.md` against `slice_diff` for the sample row only; not worth it. Fixed in the fix round with the slicer's own output pasted.
- **Sightings:** 1
- **Action:** none

### F-21 — Catch-all error label names one cause for several
- **Date:** 2026-09-17 (T-10)
- **Bin:** 3
- **Claim:** `cli.py` printed `git error:` for any `OSError | SubprocessError`, after `pipeline.py` gained non-git file I/O behind that handler. Fails closed either way; the text misleads. Split into `git error:` / `io error:` in the fix round. Boundary reviewer found it by reading; code reviewer by fault injection.
- **Sightings:** 1
- **Action:** none

### H-3 — sighting 6 (2026-09-17, T-10)
- The `NotImplementedError` stub instruction was back in the brief and worked: 3 of 4 red on assertions. The fourth (replay miss → exit 1) passed at red through the old T-01 placeholder; the reviewer proved it load-bearing at HEAD by mutation. One post-red assertion edit: "id present in `ts-review.md`" became "id in JSON findings + template text in md", because `render.py` never emits raw ids in markdown (T-09's contract). A wrong criterion, corrected in the test rather than reported as a planning finding; the reviewer judged it non-weakening and flagged it. Counts as an H-1 planning miss too: the T-10 acceptance bullet was written before T-09's template existed.

### H-4 — Environment facts in AGENTS.md copied into `.env` verbatim
- **Date:** 2026-09-17 (T-10)
- AGENTS.md gives the endpoint as `https://api.typesafe.ai/v1/systemone`; `.env` set `TYPESAFE_BASE_URL` to that, and the SDK appends `/v1/systemone` itself, so the first live call 404'd. The builder stripped the path in its shell and did not touch `.env`. Harness, unbinned: AGENTS.md should say what the SDK variable expects, not what the HTTP endpoint is. Human to fix `.env`.

### F-15 — note (2026-09-17, T-10)
- The T-08 fix held at the first two-module boundary it was built for: `pipeline.py` derives the request set from `questions_for`/`criterion_questions`, the same source `compose.py` expects, and the reviewer's dropped-question mutation produced `NEEDS_HUMAN / unanswered_questions`. No new sighting.

### F-3 — note (2026-09-17, T-10)
- `_clean_stale_outputs` moved from `cli.py` to `pipeline.py` with its bare `unlink`; neither module declares an error type so DEC-1 skips both, and `cli.main`'s `OSError` handler covers it. Boundary reviewer judged it the DEC-1 carve-out, not evasion. Not a sighting; noted because it is the same `unlink` corner as sighting 4.

### F-22 — Analysis code picks a branch by category and no test pins which
- **Date:** 2026-09-17 (T-11)
- **Bin:** 2
- **Claim:** `beta_for(severity)` maps severity → F0.5/F2; the mapping is the task's whole point, and the only tests called `f_beta(beta=...)` with literals. Same family as F-10 (shape not values), logged separately because the shape is specific: a pure function that selects a constant by category, tested only downstream of the constant. Checkable: a function whose body is `if category in (...): return A else B` with no test calling it or its caller with both categories. A mutation run finds it in seconds; the fixture reviewer found it that way.
- **Sightings:** 1 (counted with F-10 sighting 4; logged so the specific shape has a name)
- **Action:** soft — noted. If it recurs, a `mutmut`/`cosmic-ray` pass on `src/` in `make check` is the cure, not a control.

### F-23 — Silent drop of an answer type the code does not handle
- **Date:** 2026-09-17 (T-11)
- **Bin:** 2
- **Claim:** `_answer_value` returned `None` for a `Choice` answer, so the question vanished from the table with no signal. Same family as F-15 (absence reads as "did not fire") and F-1. Unreachable today (no `Choice` in the catalog); fixed to raise `CalibrateError`. Untested; the reviewer asked for a test before the first `Choice` question ships, not before merge.
- **Sightings:** 1
- **Action:** soft — fixed

### H-1 — sighting 5 (2026-09-17, T-11 critic pass)
- T-11 stored a case as a lone `diff.patch`; nothing in the tree slices a patch, only a git checkout. Its "edited acceptance test" case mapped to a `scope='check'` question with no probability to sweep. Scope item 6 (threshold commits to `questions.py`) contradicted its own Non-scope. Three planning defects, all fixed in the task file before dispatch: cases are `before/`/`after/` trees, the check ids are excluded from the table, the case swapped for `missing_type_hints_public`. Also: the task leaned on T-10's manual-QA list, a human step that had not happened; dropped.

### H-5 — Builder judgement calls the spec does not cover, surfaced not buried
- **Date:** 2026-09-17 (T-09..T-11)
- T-09: markdown `Final Notes` always `None.`. T-10: `pipeline.run` takes explicit `worktree`/`base`; an acceptance assertion corrected post-red. T-11: `severity='modifier'` gets F2; `pyproject.toml` excludes `fixtures/` from ruff/ty. Every one was disclosed in the return under "deviations", none hidden. Harness, unbinned: the "report deviations with reasons" line in the builder brief is doing real work; keep it.

### F-15 — sighting 2 (2026-09-18, RA-01)
- `cli._doctor` printed `ok` and exited 0 once `ask_all` returned, never reading the Noul it asked for. The recorded live answer to "Answer yes." was 0.46 and the acceptance test passed on it; a forged 0.02 passed too. Both reviewers found it by execution (moving `print('ok')` ahead of the call survived 16 tests). Fixed: a `check == "ping"` Noul, `noul >= 0.5` or exit 1, two tests that catch the mutation. Same claim as F-15: success read from the call returning, not from the answer's value. Sightings now 2; the task file said "prints `ok`, exit 0" and never mentioned the answer (counted with H-1 below).

### H-1 — sighting 6 (2026-09-18, RA-01..RA-04 critic pass)
- RA-01: `--env-file` named as a search location but never as a flag; failure stderr "names the four locations" with no format; the doctor fixture dir missing from `files`; success condition for `--doctor` written without checking the answer. RA-02: acceptance named `extract_brief` for a `Task` that only `parse_task` returns; `pipeline.py` in `files` with no scope bullet saying how the resolved task reaches it. RA-04: `checks.py` missing from `files` for the `_redact` rename; redacted state files vs a replay hash of the unredacted state — a real contradiction, one human decision (store the hash in `keys.json`). Fourth batch, same cause.

### F-16 — note (2026-09-18, RA-01)
- `calibrate._resolve_model` duplicated `pipeline._resolve_model` since T-11; the reviewer flagged it when RA-01 made the pipeline one public. Folded in the fix round. Origin is the T-11 batch already counted as sighting 2, so no new sighting; the pattern is one fresh instance from a control.

### F-24 — Feature reads ambient environment; the test suite inherits the developer's machine
- **Date:** 2026-09-18 (RA-01)
- **Bin:** 2
- **Claim:** a `src/` function walks cwd/HOME for config (`.env`, `~/.config`) and no autouse test fixture redirects those roots, so a test calling the entry point can pick up a real key and make a live call.
- **Sightings:** 1
- **Action:** soft — builder added `tests/conftest.py` (isolates HOME, cwd, `TYPESAFE_*`) unprompted and disclosed it. Both reviewers verified it blocks the live path. Checkable: any `os.environ`/`Path.home()`/`Path.cwd()` read in `src/` without a matching autouse fixture in `tests/conftest.py`.

### F-25 — Diagnostic path catches only the expected error type
- **Date:** 2026-09-18 (RA-01)
- **Bin:** 3
- **Claim:** `cli._doctor` catches `AskFailed` only; an unexpected exception is a raw traceback where the pipeline path prints one line. Boundary reviewer called it acceptable for a diagnostic. Taste; logged, no action.

<!--
### F-1 — <one-line description>
- **Date:** YYYY-MM-DD
- **Bin:** 2
- **Claim:** <the dislike, stated so a machine could check it>
- **Sightings:** 1
- **Action:** soft — noted, no control yet
- **Notes:** <anything surprising about the harness itself>
-->
