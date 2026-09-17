---
id: DEC-1
title: A module's own error type must not be leaked past by a raw I/O call
status: accepted
kind: negative
created: 2026-09-17
superseded_by: null
controls:
  - path: controls/fitness/io_error_wrapping.py
    type: fitness_fn
    enforcement: block
    pragma: supported
---

## Rule

In a module under `src/` that declares its own exception type (`class <X>Error(...)`
or `class <X>Failed(...)`), every call to `open`, `subprocess.run`, or a
`pathlib.Path` I/O method (`read_text`, `read_bytes`, `write_text`, `write_bytes`,
`mkdir`) must sit lexically inside a `try` somewhere between the call and its
enclosing function. A call with no enclosing `try` at all is the violation: it lets
a bare `OSError` / `subprocess.SubprocessError` escape past the module's declared
contract that its own failures surface as `<X>Error` (or `<X>Failed`).

The control does not require the `except` clause to specifically re-raise
`<X>Error` — converting to a returned failure value, as `checks.py` does, is just
as compliant. It only requires that the call not be bare.

Scope: this check covers standalone functions and closures nested inside them, not
methods on a class. See Context for why.

## Context

`docs/ledger-findings.md` F-3, three sightings:

- T-02, `taskfile.py`: `TaskFileError` was the module's one declared failure type,
  but `path.read_text()` sat with no `try` around it at all, so a missing or
  unreadable task file raised a bare `FileNotFoundError`/`PermissionError` instead.
- T-05, `cli.py`'s `_dump_state`: every other call in the function was wrapped, but
  `dump_dir.mkdir(...)` and one `path.write_text(...)` were not, so a dump
  directory that turned out to be a file produced a raw traceback instead of exit 1
  with a message.
- T-07, `ask.py`: `AskFailed` was declared as the module's one failure type, then
  `Record.save` (`mkdir`/`write_bytes`) and `Replay.load` (`read_bytes`, catching
  only `FileNotFoundError`) leaked raw `OSError` in the version under review.

All three are the same shape: a module states, in prose or in its one exception
class, "failures from here come out as `<X>Error`," and then a plain filesystem or
subprocess call quietly breaks that promise. It is exactly the kind of thing a
human catches by reading carefully and a mechanical check catches every time.

**Why methods are out of scope.** By the time this control was written, `ask.py`
had already been fixed: `Record` and `Replay` (implementations of a `Recorder`
`Protocol`) are only ever reached through a `recorder: Recorder` parameter, and the
one call site (`ask_all`'s inner `one()`) wraps `recorder.load(...)` /
`recorder.save(...)` in a `try` that converts every failure — `ReplayMiss`,
`OSError`, `TypeSafeError` — into `AskFailed`. The raw calls inside `Record.save`
and `Replay.load` are themselves unwrapped, or wrapped into an intermediate
exception (`ReplayMiss`) rather than `AskFailed` directly. Both are safe in
practice, because the *caller* closes the gap through the interface — but proving
that statically means resolving, for a call written as `recorder.load(...)`,
which concrete class answers it. An AST pass with no type information cannot do
that soundly; a name-based guess (matching `.load(...)` calls against `load`
methods anywhere in the module) would either flag this correct code or, worse, miss
a real leak in an unrelated class that happens to share a method name. Excluding
methods from this control is a stated limit on what it can prove, not a carve-out
added to make a particular file pass — a class with a raw, wholly unguarded I/O
call in a method that is *not* reached through a shared interface is a real gap
this control will not catch. If that turns out to matter in practice, it is a new
finding, not a silent widening of this one.

## Consequences

`controls/fitness/io_error_wrapping.py` walks every `.py` file under `src/`. For
each module that defines a top-level `<X>Error`/`<X>Failed` class, it finds every
`open`/`subprocess.run`/`Path` I/O call in a standalone function (not a method) and
fails if that call has no enclosing `try`, naming the file, line, function, and the
declared error type it can bypass.

Red: a scratch module declaring `ScratchError` with a bare `path.read_text()` in a
plain function — the control fails, naming the file and line.

Green: `taskfile.py`, `state.py`, `checks.py`, `slicing.py`, and `ask.py` (from the
not-yet-merged T-07 branch) all pass. `cli.py` declares no error type of its own
and is out of scope for this decision.

## Rejected alternatives

- **Require the `except` handler to specifically raise `<X>Error`.** Rejected:
  `checks.py`'s `_make_check` deliberately converts a read failure into a returned
  `CheckResult(status='fail')` rather than raising, which is at least as safe and
  is the established pattern for that module. Requiring a `raise` would fail on
  correct code. *Positive recast:* require only that the call be guarded by a
  `try`; what the handler does with the failure is the module's choice.

- **A plain grep for the call names.** Rejected: `read_text` and `write_bytes`
  appear in docstrings and in this very decision file; a grep cannot tell a call
  from a comment, or tell whether a real call is already inside a `try`. Parsing
  the AST is the only way to answer "is this call guarded" honestly.

- **Trace through the `Recorder` protocol to also check `Record`/`Replay`
  methods.** Rejected for this decision: doing it soundly needs type resolution
  this control does not have; doing it by name-matching is a heuristic that will
  misfire on any class that happens to share a method name with an unrelated one.
  Left as a documented gap rather than shipped as a guess.
