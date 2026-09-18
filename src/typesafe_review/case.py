"""Write a labelled-fixture case from a real pipeline run (RA-04, spec §9).

`write_case` turns one real `ts-review` run into the second calibration-fixture
kind `calibrate.py` understands: `state/` (redacted hunk + change states, plus
`keys.json`, the judge's checklist), `responses/` (filled by `case_recorder`, which
mirrors whatever the run's own recorder served -- live or replayed -- into the case
directory with no second network call), copies of the run's own `ts-review.md` /
`ts-review.json`, and `meta.json`. A human only has to add `labels.json` next to it
(RA-05) before `--calibrate fixtures/` counts it with the seed set.

`case.py` declares its own `CaseError`; DEC-1 applies: every `open`/`Path` I/O call
below sits inside a `try`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from typesafe_sdk import JSONContent

from typesafe_review.ask import Record, Recorder
from typesafe_review.ask import request_key as ask_request_key
from typesafe_review.checks import redact
from typesafe_review.questions import (
    CHANGE_KEY,
    Question,
    expected_change_questions,
    expected_hunk_questions,
    send_questions,
)
from typesafe_review.state import ChangeState, HunkState
from typesafe_review.taskfile import Task

__all__ = [
    'CHANGE_KEY',
    'CaseError',
    'case_recorder',
    'expected_by_key',
    'hunk_key',
    'write_case',
    'write_state_files',
]


class CaseError(Exception):
    """A case directory could not be written: a file write failed, or `expected`
    is missing an entry `write_case` needed (a hunk's own key, or `CHANGE_KEY`)."""


@dataclass(frozen=True)
class _CaseRecorder:
    """Wraps another `Recorder`. Every response it serves -- freshly fetched, or
    already cached in `inner` (a `Replay` hit) -- is also saved to `mirror`, so
    `--case` combined with `--replay` still fills `responses/` with no second
    network call; `--case` alone (a live run) just records there directly, exactly
    like a plain `Record` would."""

    inner: Recorder
    mirror: Record

    def load(self, key: str, hunk_key: str) -> bytes | None:
        cached = self.inner.load(key, hunk_key)
        if cached is not None:
            self.mirror.save(key, cached)
        return cached

    def save(self, key: str, body: bytes) -> None:
        self.mirror.save(key, body)
        self.inner.save(key, body)


def case_recorder(case_dir: Path, inner: Recorder) -> Recorder:
    """The `Recorder` `pipeline.run` must pass to `ask_all` once `--case` is
    given: every response `inner` serves also lands under `case_dir/responses/`
    (`cli.py`'s argparse validation rejects `--case` together with an explicit
    `--record` elsewhere, so `mirror` is always the one `responses/` this run
    produces)."""
    return _CaseRecorder(inner=inner, mirror=Record(case_dir / 'responses'))


def hunk_key(hunk_state: HunkState) -> str:
    """`f'{path}@{header}'` -- exactly `calibrate.hunk_key`'s format, derived
    straight from the state dict (`file.path`, `hunk.header`); no `Hunk`
    reconstruction needed (RA-04 context)."""
    return f'{hunk_state["file"]["path"]}@{hunk_state["hunk"]["header"]}'


def expected_by_key(
    task: Task | None, hunk_states: list[HunkState], change_state: ChangeState
) -> dict[str, dict[str, Question]]:
    """Every question id each state was eligible for, keyed the same way
    `keys.json` is: a hunk's own `hunk_key`, or `CHANGE_KEY` for the change-wide
    state -- built from `expected_hunk_questions`/`expected_change_questions`, the
    same functions `pipeline.py`'s own request-building calls (ledger F-15/F-16:
    one source of truth for what a state was eligible for, never a second
    hand-maintained selection)."""
    expected: dict[str, dict[str, Question]] = {
        hunk_key(hs): expected_hunk_questions(hs['file']['language'], hs['file']['is_test']) for hs in hunk_states
    }
    expected[CHANGE_KEY] = expected_change_questions(task)
    return expected


def _redact_value(value: Any) -> Any:
    """Walk `value` (a JSON-shaped state, or a piece of one) and redact every
    string it contains -- structure preserved, so redaction can never corrupt the
    JSON it is about to be written as (unlike redacting already-dumped JSON text,
    which risks eating a closing quote mid-match)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def _write_json(path: Path, data: Any) -> None:
    try:
        path.write_text(json.dumps(data, indent=2))
    except OSError as e:
        raise CaseError(f'{path}: cannot write ({e})') from e


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text)
    except OSError as e:
        raise CaseError(f'{path}: cannot write ({e})') from e


def write_state_files(dump_dir: Path, hunk_states: list[HunkState], change_state: ChangeState) -> list[tuple[str, int]]:
    """Write `hunk-NN.json`/`change.json` under `dump_dir`, redacted (RA-04): the
    one writer both `cli._dump_state` (T-01/T-05's `--dump-state`) and
    `write_case`'s own `state/` directory call, so `--dump-state`'s output and a
    case's `state/` files can never diverge into two hand-written JSON-dump code
    paths. Returns `[(filename, encoded_byte_length), ...]` in write order, for a
    caller (`cli._dump_state`) that wants to print a token estimate per file.
    """
    try:
        dump_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise CaseError(f'{dump_dir}: cannot create ({e})') from e

    written: list[tuple[str, int]] = []
    for i, hunk_state in enumerate(hunk_states, start=1):
        filename = f'hunk-{i:02d}.json'
        encoded = json.dumps(_redact_value(dict(hunk_state)), indent=2)
        _write_text(dump_dir / filename, encoded)
        written.append((filename, len(encoded)))

    change_encoded = json.dumps(_redact_value(dict(change_state)), indent=2)
    _write_text(dump_dir / 'change.json', change_encoded)
    written.append(('change.json', len(change_encoded)))
    return written


def write_case(
    case_dir: Path,
    hunk_states: list[HunkState],
    change_state: ChangeState,
    expected: dict[str, dict[str, Question]],
    review_json: dict[str, Any],
    review_md: str,
    meta: dict[str, Any],
) -> None:
    """Write one real-run case under `case_dir` (RA-04 item 1).

    `hunk_states`/`change_state` are the *unredacted* states this run actually
    sent -- `keys.json`'s `request_key`s are hashed from these, exactly matching
    `responses/`'s own keys (`case_recorder` mirrors responses keyed by the same
    hash `ask_all` computes on the unredacted request); only the copy written to
    `state/` (via `write_state_files`) is redacted. `expected` is every question id
    each key was eligible for (`expected_by_key`'s shape); `meta['model']` is the
    model the hash needs.
    """
    model = meta['model']
    state_dir = case_dir / 'state'
    write_state_files(state_dir, hunk_states, change_state)

    keys: dict[str, dict[str, Any]] = {}

    for i, hunk_state in enumerate(hunk_states, start=1):
        key = hunk_key(hunk_state)
        if key not in expected:
            raise CaseError(f'{case_dir}: no expected questions for hunk {key!r}')
        questions = expected[key]
        req_key = ask_request_key(cast(JSONContent, dict(hunk_state)), send_questions(questions), model)
        keys[key] = {'state': f'hunk-{i:02d}.json', 'questions': list(questions), 'request_key': req_key}

    if CHANGE_KEY not in expected:
        raise CaseError(f'{case_dir}: no expected questions for {CHANGE_KEY!r}')
    change_questions = expected[CHANGE_KEY]
    change_req_key = ask_request_key(cast(JSONContent, dict(change_state)), send_questions(change_questions), model)
    keys[CHANGE_KEY] = {'state': 'change.json', 'questions': list(change_questions), 'request_key': change_req_key}

    _write_json(state_dir / 'keys.json', keys)
    _write_text(case_dir / 'ts-review.md', review_md)
    _write_json(case_dir / 'ts-review.json', review_json)
    # `meta['repo']` is usually already credential-free (`pipeline._repo_identity`
    # strips `http(s)://user:pass@` before this is ever called), but `redact` here
    # is the independent second guard (RA-04 fix round 1): a worktree with some
    # other credentialed remote shape, or a future `meta` field, still can't leak.
    _write_json(case_dir / 'meta.json', _redact_value(meta))
