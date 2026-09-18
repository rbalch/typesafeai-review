"""Acceptance tests for T-11: labelled fixtures and `--calibrate`.

One test per acceptance clause in `tasks/typesafe-reviewer/T-11-calibration.md`:
metrics computed by hand, the sweep picking a known-best threshold on a synthetic
distribution, `insufficient` under 5 positives, an unknown label id exiting 1, and
`main(['--calibrate', 'fixtures'])` exiting 0 with a row per labelled model-scored
question id (replay only, against the committed `fixtures/` tree).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from typesafe_review import case, pipeline
from typesafe_review.calibrate import (
    MIN_POSITIVES,
    CalibrateError,
    build_row,
    calibrate,
    f_beta,
    main,
    precision_recall,
    sweep_best_threshold,
)
from typesafe_review.questions import Question
from typesafe_review.slicing import slice_diff
from typesafe_review.state import build_change_state, build_hunk_states, load_acceptance_tests, load_conventions
from typesafe_review.testrepo import build_two_stage_repo
from typesafe_review.verdict import EXIT_APPROVE, EXIT_TOOL_FAILURE

FIXTURES_DIR = Path(__file__).resolve().parent.parent / 'fixtures'

# ---------------------------------------------------------------------------
# 1. Metrics on a hand-built answer set match values computed by hand.
# ---------------------------------------------------------------------------
#
# labels:      T    T    T    T    T    F    F    F
# values:     .9   .8   .7   .3   .2   .6   .4   .1
# threshold=0.5, direction='ge' -> predictions:
#              T    T    T    F    F    T    F    F
# TP=3 (idx 0,1,2), FN=2 (idx 3,4), FP=1 (idx 5), TN=2 (idx 6,7)
# precision = 3/4 = 0.75, recall = 3/5 = 0.6
_LABELS = [True, True, True, True, True, False, False, False]
_VALUES = [0.9, 0.8, 0.7, 0.3, 0.2, 0.6, 0.4, 0.1]


def _predict(values: list[float], threshold: float) -> list[bool]:
    return [v >= threshold for v in values]


def test_precision_recall_matches_hand_computed_values() -> None:
    precision, recall = precision_recall(_LABELS, _predict(_VALUES, 0.5))
    assert precision == pytest.approx(0.75)
    assert recall == pytest.approx(0.6)


def test_f_beta_matches_hand_computed_values() -> None:
    precision, recall = 0.75, 0.6
    assert f_beta(precision, recall, 1.0) == pytest.approx(2 / 3)
    # F0.5 = (1+0.25)*p*r / (0.25*p + r) = 1.25*0.45 / (0.1875+0.6) = 0.5625/0.7875
    assert f_beta(precision, recall, 0.5) == pytest.approx(0.5625 / 0.7875)
    # F2 = 5*p*r / (4*p + r) = 5*0.45 / (3+0.6) = 2.25/3.6
    assert f_beta(precision, recall, 2.0) == pytest.approx(2.25 / 3.6)


def test_f_beta_zero_when_precision_and_recall_are_zero() -> None:
    assert f_beta(0.0, 0.0, 1.0) == 0.0


# ---------------------------------------------------------------------------
# 2. Sweep picks the known best threshold on a synthetic distribution.
# ---------------------------------------------------------------------------
#
# 5 positives at 0.55, 4 negatives at 0.45, 1 negative outlier at 0.75.
# Grid points 0.05..0.45 predict everyone positive (FP=5, precision=0.5, recall=1,
# F1=2/3). Grid points 0.50/0.55 predict only the outlier negative as a false
# positive (precision=5/6, recall=1, F1=10/11) -- the maximum, first hit at 0.50
# (0.55 ties but does not overtake a strictly-greater-only sweep). Grid points
# >=0.60 miss every positive (recall=0, F1=0).
_SWEEP_LABELS = [True] * 5 + [False] * 5
_SWEEP_VALUES = [0.55] * 5 + [0.45, 0.45, 0.45, 0.45, 0.75]


def test_sweep_picks_known_best_threshold() -> None:
    best = sweep_best_threshold(_SWEEP_LABELS, _SWEEP_VALUES, 'ge', 1.0)
    assert best is not None
    best_threshold, best_f = best
    assert best_threshold == pytest.approx(0.5)
    assert best_f == pytest.approx(10 / 11)


# ---------------------------------------------------------------------------
# 3. `insufficient` under 5 positives.
# ---------------------------------------------------------------------------

_TINY_QUESTION = Question(
    id='tiny_question',
    primitive=None,
    scope='hunk',
    severity='minor',
    threshold=0.75,
    direction='ge',
    fix='fix',
    issue='issue',
    why='why',
    rubric='5.2',
)


def test_insufficient_under_five_positives() -> None:
    assert MIN_POSITIVES == 5
    pairs = [(True, 0.9), (True, 0.8), (True, 0.76), (False, 0.1), (False, 0.2)]
    row = build_row(_TINY_QUESTION, pairs)
    assert row.n_positive == 3
    assert row.insufficient is True
    assert row.best_threshold is None
    assert row.best_f_beta is None


def test_sufficient_at_five_positives_gets_a_best_threshold() -> None:
    pairs = [
        (True, 0.9),
        (True, 0.85),
        (True, 0.8),
        (True, 0.78),
        (True, 0.76),
        (False, 0.1),
        (False, 0.2),
    ]
    row = build_row(_TINY_QUESTION, pairs)
    assert row.n_positive == 5
    assert row.insufficient is False
    assert row.best_threshold is not None


# ---------------------------------------------------------------------------
# Fix round 1, item 1: `build_row` must use the right F-beta for a question's own
# severity -- swapping `beta_for`'s two return values must fail this test. Reuses
# the exact (label, value) pairs from the hand-computed metrics section above
# (precision=0.75, recall=0.6) so F0.5 and F2 provably differ (0.7143 vs 0.625),
# through `build_row`, not `f_beta` called in isolation.
# ---------------------------------------------------------------------------

_MINOR_QUESTION = Question(
    id='minor_question',
    primitive=None,
    scope='hunk',
    severity='minor',
    threshold=0.5,
    direction='ge',
    fix='fix',
    issue='issue',
    why='why',
    rubric='5.2',
)

_IMPORTANT_QUESTION = Question(
    id='important_question',
    primitive=None,
    scope='hunk',
    severity='important',
    threshold=0.5,
    direction='ge',
    fix='fix',
    issue='issue',
    why='why',
    rubric='5.1',
)


def test_build_row_uses_f0_5_for_minor_severity() -> None:
    pairs = list(zip(_LABELS, _VALUES, strict=True))
    row = build_row(_MINOR_QUESTION, pairs)
    assert row.beta == 0.5
    assert row.precision == pytest.approx(0.75)
    assert row.recall == pytest.approx(0.6)
    # F0.5 = 1.25*p*r / (0.25*p + r) = 1.25*0.45 / (0.1875+0.6) = 0.5625/0.7875
    assert row.f_beta == pytest.approx(0.5625 / 0.7875)


def test_build_row_uses_f2_for_important_severity() -> None:
    pairs = list(zip(_LABELS, _VALUES, strict=True))
    row = build_row(_IMPORTANT_QUESTION, pairs)
    assert row.beta == 2.0
    assert row.precision == pytest.approx(0.75)
    assert row.recall == pytest.approx(0.6)
    # F2 = 5*p*r / (4*p + r) = 5*0.45 / (3+0.6) = 2.25/3.6
    assert row.f_beta == pytest.approx(2.25 / 3.6)
    # Sanity: the two severities must disagree, or a `beta_for` swap could still
    # slip through if F0.5 and F2 happened to coincide.
    assert row.f_beta != pytest.approx(0.5625 / 0.7875)


# ---------------------------------------------------------------------------
# Fix round 1, item 2: `build_row` must report the *swept* best threshold, not the
# catalog threshold. A distribution where the catalog threshold (0.85) scores 0 and
# the swept best (0.15) scores 25/26, computed by hand below, so a mutation that
# makes `build_row` fall back to `(question.threshold, score)` fails on both
# `best_threshold` and `best_f_beta`.
# ---------------------------------------------------------------------------
#
# severity='important' -> beta=2.0. 5 positives at 0.6, negatives at 0.1, 0.1, 0.9.
# At threshold=0.85 (catalog): only the 0.9 negative fires -> TP=0, FP=1, FN=5 ->
#   precision=0, recall=0 -> F2=0.0.
# For threshold in [0.15, 0.60]: the 0.9 negative still fires (FP=1) but the 0.1
#   negatives no longer do, and all 5 positives (0.6 >= threshold) still fire ->
#   TP=5, FP=1, FN=0 -> precision=5/6, recall=1 ->
#   F2 = 5*(5/6)*1 / (4*(5/6)+1) = (25/6) / (26/6) = 25/26 (the maximum: at
#   threshold <= 0.10 the 0.1 negatives also fire, adding FP and lowering precision
#   to 5/8, giving F2 = 25/28 < 25/26; above 0.60 the positives stop firing and
#   recall drops to 0). The sweep grid's first point in [0.15, 0.60] is 0.15.
_SWEEP_ROW_QUESTION = Question(
    id='sweep_row_question',
    primitive=None,
    scope='change',
    severity='important',
    threshold=0.85,
    direction='ge',
    fix='fix',
    issue='issue',
    why='why',
    rubric='5.1',
)
_SWEEP_ROW_PAIRS: list[tuple[bool, float]] = [
    (True, 0.6),
    (True, 0.6),
    (True, 0.6),
    (True, 0.6),
    (True, 0.6),
    (False, 0.1),
    (False, 0.1),
    (False, 0.9),
]


def test_build_row_reports_swept_best_threshold_not_catalog_threshold() -> None:
    row = build_row(_SWEEP_ROW_QUESTION, _SWEEP_ROW_PAIRS)
    assert row.n_positive == 5
    assert row.insufficient is False
    # At the catalog threshold (0.85) nothing useful fires.
    assert row.threshold == 0.85
    assert row.precision == pytest.approx(0.0)
    assert row.recall == pytest.approx(0.0)
    assert row.f_beta == pytest.approx(0.0)
    # The swept best is a different threshold with a real score.
    assert row.best_threshold == pytest.approx(0.15)
    assert row.best_f_beta == pytest.approx(25 / 26)
    assert row.best_threshold != row.threshold


# ---------------------------------------------------------------------------
# 4. An unknown label id exits 1.
# ---------------------------------------------------------------------------


def _write_python_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_case(root: Path, *, before: str, after: str, labels: dict) -> Path:
    case = root / 'case'
    _write_python_file(case / 'before' / 'pkg.py', before)
    _write_python_file(case / 'after' / 'pkg.py', after)
    (case / 'labels.json').write_text(json.dumps(labels))
    return case


_BEFORE_SRC = 'def thing():\n    return False\n'
_AFTER_SRC_WITH_SWALLOW = (
    'def thing():\n'
    '    return False\n'
    '\n\n'
    'def safe_thing():\n'
    '    try:\n'
    '        return thing()\n'
    '    except Exception:\n'
    '        return None\n'
)


def test_unknown_label_question_id_exits_1(tmp_path: Path) -> None:
    fixtures = tmp_path / 'fixtures'
    fixtures.mkdir()
    _make_case(
        fixtures,
        before=_BEFORE_SRC,
        after=_AFTER_SRC_WITH_SWALLOW,
        labels={'<change>': {'this_is_not_a_real_question_id': True}},
    )
    rc = main(['--calibrate', str(fixtures)])
    assert rc == EXIT_TOOL_FAILURE


def test_unknown_label_question_id_raises_calibrate_error(tmp_path: Path) -> None:
    fixtures = tmp_path / 'fixtures'
    fixtures.mkdir()
    _make_case(
        fixtures,
        before=_BEFORE_SRC,
        after=_AFTER_SRC_WITH_SWALLOW,
        labels={'<change>': {'this_is_not_a_real_question_id': True}},
    )
    with pytest.raises(CalibrateError):
        calibrate(fixtures)


# ---------------------------------------------------------------------------
# 5. `main(['--calibrate', 'fixtures'])` exits 0 with a row per labelled,
#    model-scored question id -- replay only, against the committed fixtures/.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not (FIXTURES_DIR / 'README.md').exists(), reason='fixtures/ not seeded yet')
def test_calibrate_committed_fixtures_replay_only() -> None:
    rows = calibrate(FIXTURES_DIR)
    assert rows
    ids = {row.question_id for row in rows}
    # every one of these ids is exercised, labelled, by at least one seed case.
    assert 'swallows_exception' in ids
    assert 'bare_except' in ids


@pytest.mark.skipif(not (FIXTURES_DIR / 'README.md').exists(), reason='fixtures/ not seeded yet')
def test_main_calibrate_fixtures_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(['--calibrate', str(FIXTURES_DIR)])
    assert rc == EXIT_APPROVE
    out = capsys.readouterr().out
    assert 'swallows_exception' in out


# ---------------------------------------------------------------------------
# RA-04: the second (state) fixture kind.
# ---------------------------------------------------------------------------

_SWALLOWS_EXCEPTION_CASE = FIXTURES_DIR / 'swallows_exception'


def _build_state_case(dest: Path, tree_case: Path) -> None:
    """Build a state-kind case under `dest` from `tree_case`'s own `before/`/
    `after/`, reusing `tree_case`'s existing `responses/` verbatim: since the
    sliced diff, task (none here) and model are identical, `write_case`'s own
    `ask_request_key` hashes land on exactly the same filenames already recorded
    there -- no live call, no re-recording."""
    built = build_two_stage_repo(dest / '_repo', tree_case / 'before', tree_case / 'after')
    change = slice_diff(built.repo, built.base_sha)
    conventions = load_conventions(built.repo)
    acceptance_tests = load_acceptance_tests(built.repo, None)
    hunk_states = build_hunk_states(None, change, conventions)
    change_state = build_change_state(None, change, acceptance_tests)
    expected = case.expected_by_key(None, hunk_states, change_state)
    model = pipeline.resolve_model()
    meta = {
        'repo': str(built.repo),
        'base': built.base_sha,
        'head': 'HEAD',
        'task_source': 'none',
        'model': model,
        'date': '2026-01-01T00:00:00+00:00',
        'verdict': 'CHANGES_REQUESTED',
        'score': 2,
    }
    case_dir = dest / 'case'
    case.write_case(case_dir, hunk_states, change_state, expected, {'ok': True}, 'md', meta)
    shutil.copytree(tree_case / 'responses', case_dir / 'responses', dirs_exist_ok=True)
    shutil.copy(tree_case / 'labels.json', case_dir / 'labels.json')


def test_state_kind_case_yields_same_rows_as_equivalent_tree_kind_case(tmp_path: Path) -> None:
    tree_fixtures = tmp_path / 'tree_fixtures'
    tree_fixtures.mkdir()
    shutil.copytree(_SWALLOWS_EXCEPTION_CASE, tree_fixtures / 'swallows_exception')
    rows_tree = {row.question_id: row for row in calibrate(tree_fixtures)}
    assert rows_tree  # sanity: the tree case itself still calibrates

    state_fixtures = tmp_path / 'state_fixtures'
    state_fixtures.mkdir()
    _build_state_case(state_fixtures, _SWALLOWS_EXCEPTION_CASE)
    rows_state = {row.question_id: row for row in calibrate(state_fixtures)}

    assert set(rows_state) == set(rows_tree)
    for question_id, tree_row in rows_tree.items():
        assert rows_state[question_id] == tree_row


def test_corrupt_state_case_response_raises_calibrate_error_naming_the_file(tmp_path: Path) -> None:
    state_fixtures = tmp_path / 'state_fixtures'
    state_fixtures.mkdir()
    _build_state_case(state_fixtures, _SWALLOWS_EXCEPTION_CASE)
    case_dir = state_fixtures / 'case'

    responses_dir = case_dir / 'responses'
    corrupted = next(responses_dir.iterdir())
    corrupted.write_bytes(b'{not valid json at all')

    with pytest.raises(CalibrateError) as excinfo:
        calibrate(state_fixtures)
    assert str(corrupted) in str(excinfo.value) or corrupted.name in str(excinfo.value)


def test_corrupt_state_case_response_exits_1_via_main(tmp_path: Path) -> None:
    state_fixtures = tmp_path / 'state_fixtures'
    state_fixtures.mkdir()
    _build_state_case(state_fixtures, _SWALLOWS_EXCEPTION_CASE)
    case_dir = state_fixtures / 'case'

    responses_dir = case_dir / 'responses'
    corrupted = next(responses_dir.iterdir())
    corrupted.write_bytes(b'{not valid json at all')

    rc = main(['--calibrate', str(state_fixtures)])
    assert rc == EXIT_TOOL_FAILURE


def test_unlabelled_case_is_skipped_and_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = tmp_path / 'fixtures'
    fixtures.mkdir()
    unlabelled = _make_case(fixtures, before=_BEFORE_SRC, after=_AFTER_SRC_WITH_SWALLOW, labels={})
    (unlabelled / 'labels.json').unlink()

    rows = calibrate(fixtures)
    assert rows == []
    err = capsys.readouterr().err
    assert 'skipped' in err
    assert str(unlabelled) in err


def test_nested_real_case_is_found(tmp_path: Path) -> None:
    fixtures = tmp_path / 'fixtures'
    real_dir = fixtures / 'real'
    real_dir.mkdir(parents=True)
    shutil.copytree(_SWALLOWS_EXCEPTION_CASE, real_dir / 'swallows_exception')

    rows = calibrate(fixtures)
    ids = {row.question_id for row in rows}
    assert 'swallows_exception' in ids
    assert 'bare_except' in ids
