"""Offline calibration against labelled fixtures (T-11, spec §9).

`calibrate(fixtures_dir)` replays every case under `fixtures_dir` (one directory per
case: `before/`, `after/`, `labels.json`, optional `task.md`, a `responses/` dir of
recorded answers) and computes precision, recall and an F-beta score at the catalog
threshold for every model-scored question id that has labels, plus the threshold
(swept 0.05..0.95) that maximises that F-beta. `run`/`main` are the CLI-facing
entry points; `cli.py`'s `--calibrate` calls `run` directly.

Every state + question set sent here is built from `questions.py`'s own
`questions_for`/`criterion_questions` -- the same functions `pipeline.py` and
`compose.py` call -- so the request set calibration measures against can never drift
from the request set a real run actually sends (ledger F-15/F-16).

Label validation happens before any replay call, per case: an unknown `labels.json`
key or question id is a `CalibrateError` raised while still only holding the sliced
`Change`, so a case with a labelling bug never needs its `responses/` fixtures to
exist to be caught.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from typesafe_sdk import Choice, JSONContent, Noul, Score, SystemOneResponse
from typesafe_sdk import constants as typesafe_constants

from typesafe_review.ask import AskFailed, AskResult, Record, Replay, RequestItem, ask_all
from typesafe_review.questions import (
    CATALOG,
    Direction,
    Question,
    Severity,
    expected_change_questions,
    expected_hunk_questions,
)
from typesafe_review.slicing import Hunk, SlicingError, slice_diff
from typesafe_review.state import (
    ChangeState,
    HunkState,
    StateError,
    build_change_state,
    build_hunk_states,
    load_acceptance_tests,
    load_conventions,
)
from typesafe_review.taskfile import Task, TaskFileError, load_task
from typesafe_review.testrepo import TestRepoError, build_two_stage_repo
from typesafe_review.verdict import EXIT_APPROVE, EXIT_TOOL_FAILURE

#: A question needs at least this many positive labels before a sweep threshold is
#: reported; below it the row is marked `insufficient` instead.
MIN_POSITIVES = 5

#: The threshold sweep, spec §9 item 2: 0.05..0.95 in steps of 0.05.
THRESHOLD_SWEEP: tuple[float, ...] = tuple(round(0.05 + 0.05 * i, 2) for i in range(19))

#: `labels.json` key for change-wide questions (matches `compose.py`'s own
#: `_CHANGE_AREA` convention, restated here because `labels.json` is this module's own
#: file format, not an import from `compose.py`).
CHANGE_KEY = '<change>'

_SEVERITY_ORDER: dict[str, int] = {'blocker': 0, 'important': 1, 'modifier': 2, 'minor': 3, 'nit': 4}


class CalibrateError(Exception):
    """A fixtures directory, a case, or a `labels.json` entry could not be turned into
    a calibration run: a missing/malformed file, an unknown label key, or an unknown
    question id.

    Never swallowed: every file read this module makes is wrapped so no bare
    `OSError`/`json.JSONDecodeError` escapes to the caller.
    """


@dataclass(frozen=True)
class QuestionRow:
    """One row of the calibration table: a question id's metrics at its catalog
    threshold, plus the best threshold the sweep found (`None` if `insufficient`)."""

    question_id: str
    severity: Severity
    threshold: float
    direction: Direction
    beta: float
    n_total: int
    n_positive: int
    precision: float
    recall: float
    f_beta: float
    insufficient: bool
    best_threshold: float | None
    best_f_beta: float | None


def classify(value: float, threshold: float, direction: Direction) -> bool:
    """Whether `value` fires `threshold` at `direction`, exactly `compose._fires`'s
    rule (restated here: `compose.py` operates on `Finding`s from real answers, this
    module operates on bare `(label, value)` pairs for metrics, so there is no shared
    call site to reuse without threading a `Question` through a bare float)."""
    if direction == 'ge':
        return value >= threshold
    return value <= threshold


def precision_recall(labels: list[bool], predictions: list[bool]) -> tuple[float, float]:
    """Precision and recall over parallel `labels`/`predictions`. `0.0` for a
    denominator of zero (no predicted positives, or no actual positives)."""
    tp = sum(1 for label, pred in zip(labels, predictions, strict=True) if label and pred)
    fp = sum(1 for label, pred in zip(labels, predictions, strict=True) if not label and pred)
    fn = sum(1 for label, pred in zip(labels, predictions, strict=True) if label and not pred)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return precision, recall


def f_beta(precision: float, recall: float, beta: float) -> float:
    """The F-beta score for `precision`/`recall`. `0.0` if both are `0.0`."""
    if precision == 0.0 and recall == 0.0:
        return 0.0
    beta_sq = beta * beta
    denominator = beta_sq * precision + recall
    if denominator == 0.0:
        return 0.0
    return (1 + beta_sq) * precision * recall / denominator


def beta_for(severity: Severity) -> float:
    """F0.5 (precision-weighted) for minor/nit, F2 (recall-weighted) for
    blocker/important/modifier (spec §9 item 2; `modifier` -- `touches_high_risk` --
    only ever escalates another question's severity, so a missed positive there is at
    least as costly as a missed blocker)."""
    if severity in ('minor', 'nit'):
        return 0.5
    return 2.0


def sweep_best_threshold(
    labels: list[bool], values: list[float], direction: Direction, beta: float
) -> tuple[float, float] | None:
    """The `(threshold, f_beta)` in `THRESHOLD_SWEEP` that maximises F-beta, or
    `None` if `labels`/`values` are empty. Ties keep the first (lowest) threshold."""
    if not labels:
        return None
    best: tuple[float, float] | None = None
    for threshold in THRESHOLD_SWEEP:
        predictions = [classify(value, threshold, direction) for value in values]
        precision, recall = precision_recall(labels, predictions)
        score = f_beta(precision, recall, beta)
        if best is None or score > best[1]:
            best = (threshold, score)
    return best


def build_row(question: Question, pairs: list[tuple[bool, float]]) -> QuestionRow:
    """One `QuestionRow` for `question` from its `(label, value)` pairs across every
    case. `insufficient=True`, `best_threshold=None` under `MIN_POSITIVES`."""
    labels = [label for label, _ in pairs]
    values = [value for _, value in pairs]
    n_total = len(pairs)
    n_positive = sum(1 for label in labels if label)
    beta = beta_for(question.severity)

    predictions = [classify(value, question.threshold, question.direction) for value in values]
    precision, recall = precision_recall(labels, predictions)
    score = f_beta(precision, recall, beta)

    insufficient = n_positive < MIN_POSITIVES
    best = None if insufficient else sweep_best_threshold(labels, values, question.direction, beta)

    return QuestionRow(
        question_id=question.id,
        severity=question.severity,
        threshold=question.threshold,
        direction=question.direction,
        beta=beta,
        n_total=n_total,
        n_positive=n_positive,
        precision=precision,
        recall=recall,
        f_beta=score,
        insufficient=insufficient,
        best_threshold=best[0] if best is not None else None,
        best_f_beta=best[1] if best is not None else None,
    )


def hunk_key(hunk: Hunk) -> str:
    """`labels.json`'s key for one hunk: `f'{hunk.path}@{hunk.header}'`. Derived from
    the sliced hunk itself, never the pipeline's positional (`hunk-{i}`) or content
    (`request_key`) keys, so a case's labels stay stable across a `slice_diff` that
    reorders or truncates unrelated hunks."""
    return f'{hunk.path}@{hunk.header}'


def load_labels(case_dir: Path) -> dict[str, dict[str, bool]]:
    """Parse `case_dir/labels.json`. Raises `CalibrateError` if it is missing,
    unreadable, or not a JSON object."""
    path = case_dir / 'labels.json'
    try:
        text = path.read_text()
    except OSError as e:
        raise CalibrateError(f'{path}: cannot read labels.json ({e})') from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CalibrateError(f'{path}: invalid JSON ({e})') from e
    if not isinstance(data, dict):
        raise CalibrateError(f'{path}: labels.json must be a JSON object')
    return data


def load_case_task(case_dir: Path) -> Task | None:
    """`case_dir/task.md` parsed into a `Task`, or `None` if the case has none."""
    task_path = case_dir / 'task.md'
    if not task_path.exists():
        return None
    try:
        return load_task(task_path)
    except TaskFileError as e:
        raise CalibrateError(str(e)) from e


def _send_questions(expected: dict[str, Question]) -> dict[str, Noul | Choice | Score]:
    return {qid: q.primitive for qid, q in expected.items() if q.primitive is not None}


def _expected_by_key(hunks: list[Hunk], task: Task | None) -> dict[str, dict[str, Question]]:
    expected: dict[str, dict[str, Question]] = {
        hunk_key(hunk): expected_hunk_questions(hunk.language, hunk.is_test) for hunk in hunks
    }
    expected[CHANGE_KEY] = expected_change_questions(task)
    return expected


def _validate_labels(
    case_dir: Path, labels: dict[str, dict[str, bool]], expected: dict[str, dict[str, Question]]
) -> None:
    for key, question_labels in labels.items():
        if key not in expected:
            raise CalibrateError(f'{case_dir}: labels.json key {key!r} does not match any hunk or {CHANGE_KEY!r}')
        for question_id in question_labels:
            if question_id not in expected[key]:
                raise CalibrateError(f'{case_dir}: labels.json has unknown question id {question_id!r} for key {key!r}')


def _resolve_model() -> str:
    return os.environ.get(typesafe_constants.DEFAULT_MODEL_ENV, typesafe_constants.DEFAULT_MODEL)


def _build_requests(
    hunks: list[Hunk], hunk_states: list[HunkState], change_state: ChangeState, task: Task | None
) -> list[RequestItem]:
    requests: list[RequestItem] = [
        (
            hunk_key(hunk),
            cast(JSONContent, dict(hunk_state)),
            _send_questions(expected_hunk_questions(hunk.language, hunk.is_test)),
        )
        for hunk, hunk_state in zip(hunks, hunk_states, strict=True)
    ]
    requests.append(
        (CHANGE_KEY, cast(JSONContent, dict(change_state)), _send_questions(expected_change_questions(task)))
    )
    return requests


def record_case(case_dir: Path, requests: list[RequestItem], *, model: str) -> AskResult:
    """Live `--record` pass for one case: send every request, save raw responses
    under `case_dir/responses/`. Never called by `calibrate`/`run`; a building block
    for `record_one_case`, the live recording pass documented in `fixtures/README.md`."""
    recorder = Record(case_dir / 'responses')
    return asyncio.run(ask_all(requests, model=model, recorder=recorder))


def record_one_case(case_dir: Path, model: str | None = None) -> AskResult:
    """One-off live `--record` pass for one fixture case: build its throwaway repo,
    slice it, build the exact request set `calibrate` would replay, and record real
    responses into `case_dir/responses/`. Never called by `calibrate`/`run` -- this is
    the entry point `fixtures/README.md`'s recording instructions call directly."""
    resolved_model = model or _resolve_model()
    task = load_case_task(case_dir)
    with tempfile.TemporaryDirectory() as tmp:
        built = build_two_stage_repo(Path(tmp), case_dir / 'before', case_dir / 'after')
        change = slice_diff(built.repo, built.base_sha)
        conventions = load_conventions(built.repo)
        acceptance_tests = load_acceptance_tests(built.repo, None)
        hunk_states = build_hunk_states(task, change, conventions)
        change_state = build_change_state(task, change, acceptance_tests)
        requests = _build_requests(change.hunks, hunk_states, change_state, task)
        return record_case(case_dir, requests, model=resolved_model)


def _replay_case(case_dir: Path, requests: list[RequestItem], *, model: str) -> dict[str, SystemOneResponse]:
    recorder = Replay(case_dir / 'responses')
    result = asyncio.run(ask_all(requests, model=model, recorder=recorder))
    return dict(result.answers)


def _answer_value(answers: SystemOneResponse, question_id: str) -> float | None:
    """`None` means "unanswered" (not in the response at all) -- the caller's own
    signal to skip that sample. A `Choice` answer is a different case: it exists but
    this module has no metric for it. `compose.py`'s `_assert_supported_primitives`
    treats a `Choice` in an expected set as a catalog bug for the same reason; here it
    would otherwise silently vanish from the table instead of erroring, a fail-open
    path this module's own contract (every file read wrapped, nothing swallowed)
    rules out."""
    if question_id in answers.nouls:
        return answers.nouls[question_id].noul
    if question_id in answers.scores:
        return answers.scores[question_id].score
    if question_id in answers.choices:
        raise CalibrateError(f'{question_id!r}: Choice answers have no calibration metric yet')
    return None


def calibrate(fixtures_dir: Path) -> list[QuestionRow]:
    """Replay every case under `fixtures_dir` and return one `QuestionRow` per
    model-scored question id that has at least one label anywhere.

    Raises `CalibrateError` for an unknown label key or question id, and `AskFailed`
    (from `ask.py`) for a replay miss -- both propagate to `run`, which turns them
    into exit 1.
    """
    try:
        case_dirs = sorted(p for p in fixtures_dir.iterdir() if p.is_dir())
    except OSError as e:
        raise CalibrateError(f'{fixtures_dir}: cannot list fixture cases ({e})') from e

    model = _resolve_model()
    #: Every question definition seen: the static catalog plus, per case with a
    #: `task.md`, that case's dynamic `criterion_*` ids -- accumulated as cases are
    #: walked so `build_row` always has the `Question` (severity/threshold/direction)
    #: a sampled id came from, catalog or per-task alike.
    questions_by_id: dict[str, Question] = {q.id: q for q in CATALOG if q.primitive is not None}
    samples: dict[str, list[tuple[bool, float]]] = {}

    for case_dir in case_dirs:
        if not (case_dir / 'labels.json').exists():
            continue

        labels = load_labels(case_dir)
        task = load_case_task(case_dir)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            try:
                built = build_two_stage_repo(tmp_root, case_dir / 'before', case_dir / 'after')
            except TestRepoError as e:
                raise CalibrateError(str(e)) from e

            try:
                change = slice_diff(built.repo, built.base_sha)
            except SlicingError as e:
                raise CalibrateError(str(e)) from e

            expected = _expected_by_key(change.hunks, task)
            _validate_labels(case_dir, labels, expected)
            for question_map in expected.values():
                questions_by_id.update(question_map)

            try:
                conventions = load_conventions(built.repo)
                acceptance_tests = load_acceptance_tests(built.repo, None)
                hunk_states = build_hunk_states(task, change, conventions)
                change_state = build_change_state(task, change, acceptance_tests)
            except StateError as e:
                raise CalibrateError(str(e)) from e

            requests = _build_requests(change.hunks, hunk_states, change_state, task)
            answers_by_key = _replay_case(case_dir, requests, model=model)

        for key, question_map in expected.items():
            answers = answers_by_key.get(key)
            if answers is None:
                continue
            case_labels = labels.get(key, {})
            for question_id in question_map:
                value = _answer_value(answers, question_id)
                if value is None:
                    continue
                label = case_labels.get(question_id, False)
                samples.setdefault(question_id, []).append((label, value))

    rows = [build_row(questions_by_id[question_id], pairs) for question_id, pairs in samples.items()]
    rows.sort(key=lambda row: (_SEVERITY_ORDER.get(row.severity, 99), row.question_id))
    return rows


def render_table(rows: list[QuestionRow]) -> str:
    """Render `rows` as a Markdown table, sorted blocker/important/modifier first."""
    header = (
        '| question | severity | n | pos | precision | recall | F-beta @ threshold | '
        'best threshold | best F-beta |\n'
        '|---|---|---|---|---|---|---|---|---|'
    )
    lines = [header]
    for row in rows:
        best_threshold = 'insufficient' if row.insufficient else f'{row.best_threshold:.2f}'
        best_f_beta = 'insufficient' if row.insufficient else f'{row.best_f_beta:.3f}'
        lines.append(
            f'| {row.question_id} | {row.severity} | {row.n_total} | {row.n_positive} | '
            f'{row.precision:.3f} | {row.recall:.3f} | {row.f_beta:.3f} @ {row.threshold} | '
            f'{best_threshold} | {best_f_beta} |'
        )
    return '\n'.join(lines)


def run(fixtures_dir: Path) -> int:
    """`--calibrate fixtures_dir`: print the table to stdout, return 0, or print an
    error to stderr and return 1 (spec §6.7's exit-code convention, restated for this
    module's own error family)."""
    try:
        rows = calibrate(fixtures_dir)
    except (CalibrateError, AskFailed, TaskFileError, SlicingError, StateError, TestRepoError) as e:
        print(str(e), file=sys.stderr)
        return EXIT_TOOL_FAILURE
    print(render_table(rows))
    return EXIT_APPROVE


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point: `main(['--calibrate', 'fixtures'])`. `cli.py`'s own
    `--calibrate` flag calls `run` directly instead of re-parsing argv through here."""
    parser = argparse.ArgumentParser(prog='ts-review --calibrate')
    parser.add_argument('--calibrate', type=Path, required=True, dest='calibrate')
    args = parser.parse_args(argv)
    return run(args.calibrate)


if __name__ == '__main__':
    sys.exit(main())
