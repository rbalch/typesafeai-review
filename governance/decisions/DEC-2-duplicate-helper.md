---
id: DEC-2
title: A helper duplicated verbatim across modules must be shared, not copied
status: accepted
kind: negative
created: 2026-09-18
superseded_by: null
controls:
  - path: controls/fitness/duplicate_helper.py
    type: fitness_fn
    enforcement: block
    pragma: supported
---

## Rule

No two module-level function definitions under `src/typesafe_review/`, in two
different files, may have identical bodies once cosmetic differences are stripped
away: the function's own name, its docstring, its parameter/return type
annotations, and the literal spelling of names it binds *locally* (parameters,
assignment targets, comprehension variables, `except ... as name`, `with ... as
name`, walrus targets). Two functions whose only difference is whether a
comprehension variable is called `question_id` or `qid` are the same function.

Names that are read but never locally assigned -- a call to a global helper, an
attribute chain like `typesafe_constants.DEFAULT_MODEL_ENV` -- keep their literal
spelling and are not normalised away. A function that calls a different helper, or
reads a different constant, is not a duplicate.

Trivial bodies (below 12 AST nodes once normalised -- roughly a single `return`,
`pass`, or one-line passthrough) are exempt: a coincidental match that short
carries no real logic worth sharing, and flagging it would be noise, not signal.

Scope: only module-level function definitions are compared, not methods and not
functions nested inside another function. See Context for why.

## Context

`docs/ledger-findings.md` F-16, three sightings, each the same shape: a function
another module already has gets typed in again under a private name instead of
being imported.

- T-08: `compose.py` re-declared three of `slicing.py`'s private header-parsing
  regexes to reach a symbol `HunkState` did not carry. Fixed by giving `slicing.py`
  a public `symbol_from_header()`.
- T-11: `_expected_hunk_questions`/`_expected_change_questions` existed,
  differently named but doing the same wrapping, in `compose.py`, `pipeline.py`
  *and* `calibrate.py`; `_git`/`_rev_parse` were copied from the sample-repo
  builder into `testrepo.py`. Fixed with two public functions in `questions.py`
  and `testrepo.py`'s own git helpers imported by `build_repo.py`.
- RA-04: `calibrate.py` copied `ask.py`'s `SystemOneResponse.from_http_response`
  call without its `except`; `_send_questions` existed verbatim in `pipeline.py`,
  `case.py` and `calibrate.py` (`questions.CHANGE_KEY` was also re-declared in
  `case.py` to dodge an import cycle). Fixed with `ask.response_from_bytes`,
  `questions.send_questions`, `questions.CHANGE_KEY`.

The same pattern recurred a fourth time on `develop`, unprompted, while this
decision was being authored: `pipeline.py` and `calibrate.py` each declared their
own private `_resolve_model()` -- identical bodies, same name, only the RA-01
branch (not yet merged at the time) had deduplicated it. Fixed here in the same
commit as this decision (`pipeline.resolve_model` made public, `calibrate.py`
imports it) alongside `_send_questions` (moved to `questions.py`, both call sites
updated) -- the exact remedy already landed independently on the RA-04 branch,
confirmed by running this control against that branch's tip before and after
planting a fresh copy of `send_questions` in `pipeline.py`.

All four sightings, and the one found while authoring this decision, are variable
naming aside: identical logic, typed twice. A human catches it by noticing the
second copy looks familiar; a mechanical AST comparison catches it every time,
including when the copy's local variable names were changed specifically to make
it *not* look identical on a text diff.

**Why methods are out of scope.** Two classes implementing the same small
`Protocol` method commonly and correctly share a short body -- `Record.save` and
`Replay.load`-shaped pairs are exactly the case DEC-1 already carved out for the
same reason. Comparing methods would either false-positive on every
multi-implementation interface, or need a name-matching heuristic ("only compare
methods with the same name") that misfires on unrelated classes that happen to
share a method name. Excluding methods is a stated limit, not a carve-out to pass
a particular file: a class with a raw duplicated method body outside a shared
interface is a real gap this control will not catch.

**Why a size floor.** A `MIN_NODES` floor keeps the control from flagging
one-liners (`return None`, `pass`, `return self._x`) that coincide by chance and
carry nothing worth sharing. Set below the smallest real sighting so it never
exempts an actual duplicate.

## Consequences

`controls/fitness/duplicate_helper.py` walks every `.py` file under
`src/typesafe_review/`, normalises every module-level function's body (own name,
docstring, annotations, and locally-bound names stripped; free/global names kept
literal), and groups functions by that normalised form. Any group whose members
span more than one file is a violation, reported with every file, line, and
function name involved, and whether the names matched or differed.

Red: a scratch copy of `questions.send_questions` planted in `pipeline.py` under
the name `_send_questions` -- the control fails, naming both files and both
function names (proven against the RA-04 branch tip, `c0079b4`, in the control
authorship worktree; removing the copy restores green).

Green: `src/typesafe_review/` on `develop` after this decision's own fix round
(`pipeline.resolve_model` made public and imported by `calibrate.py`;
`questions.send_questions` replacing both modules' private copies) -- proven by
running the control before the fix (red, naming both `_resolve_model`s) and after
(green).

## Rejected alternatives

- **Compare methods too, by matching method name across classes.** Rejected: a
  static, type-free AST pass cannot tell a `Protocol` implementation pair (correct,
  DEC-1's own carve-out) from a real duplicate without resolving which concrete
  class a call site reaches. A name-matching heuristic over methods would
  false-positive on every interface with two implementations.

- **A plain text/hash diff of function source.** Rejected: the RA-04 sighting was
  a fresh copy with variable names deliberately changed (`qid`/`q` instead of
  `question_id`/`question`) -- identical logic, different text. A textual or
  whole-source hash comparison misses exactly the case that prompted this
  decision; only a normalised AST comparison catches a rename-to-dodge-the-diff.

- **No size floor, flag every match.** Rejected: without it, two unrelated
  one-line functions that both `return None` or both `pass` would fail the build
  on correct code -- the single worst outcome this harness can produce. A floor
  set below the smallest real sighting loses nothing the rule was meant to catch.

- **Enforcement: warn instead of block.** Rejected: F-16 already reached its third
  sighting and a documented, already-agreed remedy exists for every case found so
  far (expose one function, import it). A rule this well-understood earns `block`,
  not an advisory note nobody is obliged to read.
