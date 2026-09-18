"""End-to-end pipeline: spec §4 steps 0-7, wired in one function.

`run(args, worktree, base)` is the only entry point `cli.py` calls, once it has
resolved `args.worktree` to a validated git worktree root and `args.base` to a base
ref (both already exercised by T-01's tests, so that resolution stays in `cli.py`
rather than being duplicated here). Steps: clean the stale outputs, run the
deterministic checks, slice the diff, build state, ask Jev (every hunk, then the one
change-wide state, in a single `ask_all` fan-out), compose the `Review`, then write
the outputs. Every step logs one stderr line before it starts (spec §3.1 "Progress").

This module raises `TaskFileError` (T-02), `SlicingError` (T-04/05), `StateError`
(T-05), `AskFailed` (T-07), `ComposeInvariantError` (T-08) and `RenderError` (T-09)
straight through to the caller -- it never catches them. `cli.py.main` is the only
place that turns one of those, or a bare `OSError`/`subprocess.SubprocessError`, into
a stderr message and exit 1 (spec §6.7); mapping them here too would just be a second
place restating the same table.

`_send_questions` is the one seam that has to agree with `compose.py`'s own idea of
what a hunk or the change should have been asked (ledger F-15): both call the exact
same `questions_for`/`criterion_questions` from `questions.py`, so the request set
this module sends and the expected set `compose.py` checks answers against can never
drift apart into two hand-maintained lists.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import cast

from typesafe_sdk import Choice, JSONContent, Noul, Score
from typesafe_sdk import constants as typesafe_constants

from typesafe_review.ask import AskResult, Live, Record, Recorder, Replay, RequestItem, ask_all
from typesafe_review.checks import run_checks
from typesafe_review.compose import Context, compose
from typesafe_review.questions import Question as CatalogQuestion
from typesafe_review.questions import expected_change_questions, expected_hunk_questions
from typesafe_review.render import OUTPUT_JSON, OUTPUT_MD, render_json, render_markdown, write_outputs
from typesafe_review.slicing import MAX_HUNK_LINES, Change, slice_diff
from typesafe_review.state import (
    ChangeState,
    HunkState,
    build_change_state,
    build_hunk_states,
    load_acceptance_tests,
    load_conventions,
)
from typesafe_review.taskfile import Task, load_task
from typesafe_review.verdict import EXIT_APPROVE, EXIT_CHANGES_REQUESTED, EXIT_NEEDS_HUMAN, Verdict

_EXIT_BY_VERDICT = {
    Verdict.APPROVE: EXIT_APPROVE,
    Verdict.CHANGES_REQUESTED: EXIT_CHANGES_REQUESTED,
    Verdict.NEEDS_HUMAN: EXIT_NEEDS_HUMAN,
}

_CHANGE_KEY = 'change'


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def _clean_stale_outputs(worktree: Path) -> None:
    """Step 0: delete `ts-review.md` / `ts-review.json` at the worktree root, if
    present -- unconditionally, before any step that can fail, so a failing run never
    leaves a previous run's outputs looking current (spec §3.1, §6.7)."""
    for name in (OUTPUT_MD, OUTPUT_JSON):
        candidate = worktree / name
        if candidate.exists():
            candidate.unlink()


def _resolve_model() -> str:
    return os.environ.get(typesafe_constants.DEFAULT_MODEL_ENV, typesafe_constants.DEFAULT_MODEL)


def _resolve_recorder(args: argparse.Namespace) -> Recorder:
    if args.replay is not None:
        return Replay(args.replay)
    if args.record is not None:
        return Record(args.record)
    return Live()


def _send_questions(expected: dict[str, CatalogQuestion]) -> dict[str, Noul | Choice | Score]:
    """The subset of an expected question set that actually goes to the model:
    every question with a primitive. The three deterministic-check ids (§4.1) carry
    `primitive=None` -- they never leave `checks.py` -- so they never reach here."""
    return {
        question_id: question.primitive for question_id, question in expected.items() if question.primitive is not None
    }


def _hunk_request_key(index: int) -> str:
    return f'hunk-{index}'


def _build_requests(task: Task | None, hunk_states: list[HunkState], change_state: ChangeState) -> list[RequestItem]:
    # `cast`: `RequestItem`'s state is `JSONContent`; `HunkState`/`ChangeState` are open
    # `TypedDict`s that serialise to exactly that shape (they round-trip through
    # `json.dumps` already, in `cli.py`'s `--dump-state`), but ty's structural check on
    # an open TypedDict cannot see that on its own.
    requests: list[RequestItem] = [
        (
            _hunk_request_key(i),
            cast(JSONContent, dict(hunk_state)),
            _send_questions(expected_hunk_questions(hunk_state['file']['language'], hunk_state['file']['is_test'])),
        )
        for i, hunk_state in enumerate(hunk_states)
    ]
    requests.append(
        (_CHANGE_KEY, cast(JSONContent, dict(change_state)), _send_questions(expected_change_questions(task)))
    )
    return requests


def _collect_notes(change: Change, conventions: str) -> list[str]:
    """Notes for `render_json`: truncations from slicing, plus state gaps (spec item
    6 of the task, not a spec section of its own)."""
    notes: list[str] = []
    for hunk in change.hunks:
        if hunk.truncated:
            notes.append(f'{hunk.path} {hunk.header}: hunk diff truncated to {MAX_HUNK_LINES} lines')
    if '[truncated]' in change.src_diff:
        notes.append('src_diff truncated to the change-wide size cap')
    if not conventions:
        notes.append(
            "AGENTS.md is missing, or has none of 'Architectural shape'/'Always'/'Never'; conventions is empty"
        )
    return notes


def run(args: argparse.Namespace, worktree: Path, base: str) -> int:
    """Steps 0-7 against `worktree`, diffing `base..HEAD`. Returns the exit code for
    `review.verdict` (spec §3: 0 APPROVE, 2 CHANGES_REQUESTED, 3 NEEDS_HUMAN)."""
    _clean_stale_outputs(worktree)

    task: Task | None = None
    if args.task is not None:
        task = load_task(args.task)

    _log('checks…')
    check_report = run_checks(worktree, args.red_sha)

    _log('slice…')
    change = slice_diff(worktree, base)

    _log('state…')
    conventions = load_conventions(worktree)
    acceptance_tests = load_acceptance_tests(worktree, args.red_sha)
    hunk_states = build_hunk_states(task, change, conventions)
    change_state = build_change_state(task, change, acceptance_tests)

    _log('ask…')
    requests = _build_requests(task, hunk_states, change_state)
    model = _resolve_model()
    recorder = _resolve_recorder(args)
    ask_result: AskResult = asyncio.run(ask_all(requests, model=model, recorder=recorder))

    hunk_answers = [(hunk_states[i], ask_result.answers[_hunk_request_key(i)]) for i in range(len(hunk_states))]
    change_answers = ask_result.answers[_CHANGE_KEY]

    _log('compose')
    context = Context(task=task, red_sha=args.red_sha, src_diff=change.src_diff, test_diff=change.test_diff)
    review = compose(check_report, hunk_answers, change_answers, context)

    _log('write')
    notes = _collect_notes(change, conventions)
    md = render_markdown(review)
    json_obj = render_json(review, ask_result, worktree, base, notes)
    write_outputs(worktree, md, json_obj)

    return _EXIT_BY_VERDICT[review.verdict]
