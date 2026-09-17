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
- **Sightings:** 1
- **Action:** soft — fixed in T-01 (compare `--show-toplevel` to the path), no control
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
- **Sightings:** 1
- **Action:** soft — fixed in T-02; watch `state.py` (T-05) and `checks.py` (T-04), which read files and run subprocesses
- **Notes:** Orchestrator and code reviewer found it independently; boundary reviewer did not. Reviewer proposed Bin 3; binned as 2 because a grep can find it.

### F-4 — Regex `match=` given an unescaped path string
- **Date:** 2026-09-17 (T-02)
- **Bin:** 3
- **Claim:** `pytest.raises(..., match=str(p))` treats a filesystem path as a regex. Harmless with tmp_path today.
- **Sightings:** 1
- **Action:** fixed by the orchestrator in the squash (`re.escape`); no ruff rule covers it, not worth one

### H-1 — Harness: planner task files restated spec and repo facts, and drifted
- **Date:** 2026-09-17 (pre-build critic pass)
- **Bin:** harness, unbinned
- **Claim:** The 11 task files carried facts that contradicted the spec or the tree: T-01 said the repo had no governance harness and told the builder to rewrite `Makefile`; T-10 told the builder to *create* this ledger file; T-04 and T-06 both owned severity for the same finding ids; T-06/T-08 assumed one Score direction while spec §5.5 fires low; T-11 could not run because T-01 required `--worktree`. Eight task-critic runs plus an orchestrator read caught 6 blockers and ~20 nits before any code.
- **Sightings:** 1 (one plan)
- **Action:** all fixed in the task files and spec (commit a07aee3); `docs/runs.md` created as the home for run metrics so this file stays orchestrator-only
- **Notes:** The critic pass earned its keep on the first plan. One critic finding was false (claimed `SystemOneResponse` lacks `.nouls/.scores/.choices`; they are properties, so struct-field introspection missed them). Verify SDK claims by calling, not by listing fields.

<!--
### F-1 — <one-line description>
- **Date:** YYYY-MM-DD
- **Bin:** 2
- **Claim:** <the dislike, stated so a machine could check it>
- **Sightings:** 1
- **Action:** soft — noted, no control yet
- **Notes:** <anything surprising about the harness itself>
-->
