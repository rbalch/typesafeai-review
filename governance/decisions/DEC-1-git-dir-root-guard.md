---
id: DEC-1
title: git rev-parse --git-dir never stands in as a repo-root guard
status: accepted
kind: negative
created: 2026-09-17
superseded_by: null
controls:
  - path: controls/fitness/git_dir_root_guard.py
    type: fitness_fn
    enforcement: block
    pragma: supported
---

## Rule
No file under `src/` may call `git rev-parse` with `--git-dir` among its literal
arguments unless the same file also calls `git rev-parse` with `--show-toplevel`.
`--git-dir` proves a path is *inside* a git repository; it says nothing about
whether the path is the repository *root*. Code that needs the root must ask for it
with `--show-toplevel` and compare the resolved path to the one it was given.

## Context

Ledger finding F-1 (`docs/ledger-findings.md`) reached its third sighting on
2026-09-17. Its first sighting, in T-01, was exactly this: `_worktree_root_error`
in `cli.py` used `git rev-parse --git-dir`, which returns success for *any* path
inside a repository, not only its root. A subdirectory passed as `--worktree`
therefore passed the guard, and the code that followed — which assumes it is
sitting at the worktree root — deleted `sub/review.md` instead of refusing the
input. The bug was fixed before this control existed: `cli.py` now calls
`git rev-parse --show-toplevel` and compares the resolved path to
`path.resolve()` (see `_worktree_root_error`). This decision keeps that fix from
regressing and gives it a name.

The finding's other two sightings — `returncode != 0` from pytest read as "tests
failed" when exit 5 means "no tests collected", and a bare `except X: return
default` around a git call — are **not** covered by this decision or its control.
They are the same *shape* of mistake (a check broader than what the code that
follows relies on) but not the same *mechanism*, and neither survives an attempt to
write a control for it without firing on correct code already in this tree:

- `checks.py` contains two call sites that compare a pytest subprocess's
  `returncode` against `0`: `_red_proof` (via the explicit set
  `_PYTEST_RED_EXIT_CODES = {1, 2}`, already correct) and `_green_at_head`, which
  uses a plain `if code != 0: return fail`. The second one is *correct* — exit 5
  ("no tests collected") must also fail a "green at HEAD" check, because there is
  nothing green to report. A control that flags "`returncode != 0` after invoking
  pytest, with no reference to exit code 5" would fail on `_green_at_head` today,
  on code that is right. The same syntax is a bug in one call site and correct in
  the other; telling them apart requires knowing what the surrounding code is
  trying to prove, which is exactly the judgment a fitness function cannot make.
- `except X: return default` around a git call is not a fixed syntactic shape
  either — sometimes returning a default on failure is the correct fail-open
  behaviour (e.g. "couldn't determine X, so don't apply this optional
  optimisation"), and sometimes it is fail-open where the code must fail closed.
  `checks.py`'s own `_run` deliberately narrows `except OSError` to "could not
  start the subprocess" and re-raises `CheckError`, while `subprocess.TimeoutExpired`
  is caught and turned into a `(None, output)` return — a default that is *correct*
  because callers check for `None` explicitly. A control banning "except + return
  default" would fire on that, too.

Both would be a control that fires on correct code — the harness's named worst
failure mode (`docs/governance-harness.md`) — so this decision does not attempt
them. The general claim behind F-1, "a guard broader than what follows relies on,"
is real and worth catching in review, but it is Bin 3 for a fitness function: it
needs to know what the following code assumes, which is not visible from the
guard's syntax alone. `--git-dir` vs `--show-toplevel` is the one sighting narrow
enough to survive: the flag itself is the tell, `--git-dir` has no other use in this
codebase (every git call here operates against a caller-supplied worktree path
whose root is load-bearing), and the fix is a fixed, greppable shape.

## Consequences

`controls/fitness/git_dir_root_guard.py` parses every `.py` file under `src/` and
flags any list/tuple literal that calls `git rev-parse` with `--git-dir`, unless
`--show-toplevel` is also called somewhere in the same file. It is a file-level
pairing rather than a per-call one — coarser than proving the two calls guard the
same branch — traded for staying a pure syntax check with no false positives against
the one legitimate use of `--git-dir` this codebase has (there is none currently;
if one is ever added deliberately, mark it `enforcement: warn` in a superseding
decision rather than looping the control).

Red: any file under `src/` that calls `git rev-parse --git-dir` with no
`--show-toplevel` call anywhere in the file. Green: today's `cli.py`, and any file
with no such call at all.

## Rejected alternatives

- **A single control covering all three F-1 sightings** (the pytest exit code and
  the bare `except` clause too). Rejected — demonstrated above to fire on correct
  code in `checks.py` as it stands today. Positive recast: ship the one sighting
  that is a pure syntax tell, and record the other two as still open, reviewed by a
  human, not a control.
- **Per-call pairing** (require `--show-toplevel` in the same function or the same
  `try` block as `--git-dir`, rather than anywhere in the file). Rejected for now —
  more precise, but needs control flow analysis (which call's result feeds which
  branch) that is easy to get wrong quietly. The file-level check already catches
  the one instance on record and every file in `src/` is small; revisit if a large
  file with multiple unrelated git helpers makes file-level pairing too coarse.
- **Ban `--git-dir` outright, anywhere, no pairing.** Rejected — `--git-dir` alone
  is not always a bug; a helper that only ever needs "is this inside a repo at
  all" is a legitimate, narrower need than root detection. Banning the flag itself
  would be encoding "we don't happen to need this today" as a permanent rule.
