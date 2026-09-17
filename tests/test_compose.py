"""Acceptance tests for T-08: compose findings, verdict and score from answers.

One test (or table row) per acceptance clause in
tasks/typesafe-reviewer/T-08-compose.md, plus the fix-round-1 additions the two
reviewers asked for (unanswered-question gating, unsupported-primitive guard, and
the pinned mutant-killing rows). Answers are built by hand with the factories below --
no fixtures, no network, no model call anywhere in this file.

Most scenarios build a *fully answered* hunk/change response via `full_hunk_answers` /
`full_change_answers`: compose.py now treats any catalog id it expected an answer for,
and did not get one, as `uncertain` with `stop_reason='unanswered_questions'`. Filling
in every id with a value that cannot fire keeps unrelated tests from tripping that
gate; a test that cares about the gate itself uses `exclude=` or an empty response.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from typesafe_sdk import Choice, NoulAnswer, Score, ScoreAnswer, SystemOneResponse, Usage

import typesafe_review.compose as compose_module
import typesafe_review.questions as questions_module
from typesafe_review.checks import CheckFinding, CheckReport, CheckResult
from typesafe_review.compose import (
    UNANSWERED,
    ComposeInvariantError,
    Context,
    Finding,
    Review,
    check_hard_constraints,
    compose,
)
from typesafe_review.questions import CATALOG, Question, Scope, Severity, criterion_questions, questions_for
from typesafe_review.state import HunkState
from typesafe_review.taskfile import Task
from typesafe_review.verdict import Verdict

# ---------------------------------------------------------------------------
# Independent pin of spec §5's per-scope id sets, transcribed by hand from
# docs/specs/typesafe-reviewer.md §5.1-§5.5 -- NOT derived from `questions_for`.
# The unanswered-question gate (compose.py) computes its own expected set from
# `questions_for`/`criterion_questions`; a test that rebuilds the same expectation
# the same way can never disagree with a bug in that computation. These literals
# are the independent check: fix-round-2 item 2.
# ---------------------------------------------------------------------------

_SPEC_5_1_IDS = frozenset(
    {
        'swallows_exception',
        'bare_except',
        'success_on_unverified',
        'indistinguishable_default',
        'auth_passes_on_error',
        'credential_in_output',
    }
)
_SPEC_5_2_IDS = frozenset(
    {
        'duplicates_helper',
        'violates_convention',
        'magic_constant',
        'vague_error_message',
        'mixed_responsibility',
    }
)
_SPEC_5_3_IDS = frozenset(
    {
        'class_is_a_function',
        'init_only_assigns',
        'all_static_class',
        'speculative_polymorphism',
        'self_as_config_bag',
        'too_many_positional_params',
        'boolean_flag_param',
        'module_level_mutable_state',
        'temporal_coupling_unenforced',
        'kwargs_passthrough_undocumented',
        'missing_type_hints_public',
        'missing_type_hints_private',
        'abc_over_protocol',
        'os_path_over_pathlib',
        'format_over_fstring',
        'constants_over_enum',
    }
)
_SPEC_5_5_HUNK_TEST_IDS = frozenset({'patches_unit_under_test', 'mock_hides_integration', 'unittest_testcase_style'})
_SPEC_5_4_AND_5_5_CHANGE_IDS = frozenset(
    {'tests_fitted_to_code', 'scope_creep', 'new_behaviour_untested', 'touches_high_risk'}
)

_SPEC_HUNK_BASE_IDS = _SPEC_5_1_IDS | _SPEC_5_2_IDS
_SPEC_PYTHON_NON_TEST_HUNK_IDS = _SPEC_HUNK_BASE_IDS | _SPEC_5_3_IDS
_SPEC_PYTHON_TEST_HUNK_IDS = _SPEC_HUNK_BASE_IDS | _SPEC_5_5_HUNK_TEST_IDS
_SPEC_NON_PYTHON_HUNK_IDS = _SPEC_HUNK_BASE_IDS

assert len(_SPEC_PYTHON_NON_TEST_HUNK_IDS) == 27
assert len(_SPEC_PYTHON_TEST_HUNK_IDS) == 14
assert len(_SPEC_NON_PYTHON_HUNK_IDS) == 11

_CATALOG_BY_ID = {q.id: q for q in CATALOG}

# ---------------------------------------------------------------------------
# Factories -- plain data, no fixtures.
# ---------------------------------------------------------------------------


def noul(value: float) -> NoulAnswer:
    return NoulAnswer(noul=value)


def score(value: float, confidence: float) -> ScoreAnswer:
    return ScoreAnswer(score=value, confidence=confidence, legend={0: 'legend'}, probabilities={0: 1.0})


def make_answers(**answers: NoulAnswer | ScoreAnswer) -> SystemOneResponse:
    return SystemOneResponse(model='jev-latest', usage=Usage(input_tokens=10), answers=dict(answers))


def _safe_value(question: Question) -> float:
    """A value for `question` that never fires and never lands in a grey zone."""
    if question.direction == 'ge':
        return 0.0
    if isinstance(question.primitive, Score):
        return float(len(question.primitive.criteria) - 1)
    return 1.0


def _default_answer(question: Question) -> NoulAnswer | ScoreAnswer:
    value = _safe_value(question)
    if isinstance(question.primitive, Score):
        confidence = max(question.confidence_min or 0.0, 0.9)
        return ScoreAnswer(score=value, confidence=confidence, legend={0: 'legend'}, probabilities={0: 1.0})
    return NoulAnswer(noul=value)


def _answers_for_ids(
    ids: frozenset[str],
    extra_catalog: dict[str, Question] | None = None,
    overrides: dict[str, NoulAnswer | ScoreAnswer] | None = None,
) -> SystemOneResponse:
    """Answer exactly `ids`, each with a value that cannot fire, looked up in the
    real catalog (plus `extra_catalog` for per-task criterion ids). Built from the
    hand-transcribed `_SPEC_*_IDS` sets above, not from `questions_for`, so it is an
    independent check on which ids `compose.py` expects for a given hunk or change.
    """
    lookup = {**_CATALOG_BY_ID, **(extra_catalog or {})}
    answers = {question_id: _default_answer(lookup[question_id]) for question_id in ids}
    answers.update(overrides or {})
    return make_answers(**answers)


def full_hunk_answers(
    hunk_state: HunkState,
    overrides: dict[str, NoulAnswer | ScoreAnswer] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> SystemOneResponse:
    """Every question `questions_for('hunk', ...)` expects for `hunk_state`, answered
    with a value that cannot fire, plus `overrides`, minus `exclude`.
    """
    expected = questions_for('hunk', language=hunk_state['file']['language'], is_test=hunk_state['file']['is_test'])
    answers: dict[str, NoulAnswer | ScoreAnswer] = {
        question_id: _default_answer(question)
        for question_id, question in expected.items()
        if question_id not in exclude
    }
    answers.update(overrides or {})
    return make_answers(**answers)


def full_change_answers(
    task: Task | None,
    overrides: dict[str, NoulAnswer | ScoreAnswer] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> SystemOneResponse:
    """Every question `questions_for('change')` (plus `criterion_questions(task)` when
    `task` is given) expects, answered with a value that cannot fire.
    """
    expected: dict[str, Question] = dict(questions_for('change'))
    if task is not None:
        expected.update(criterion_questions(task))
    answers: dict[str, NoulAnswer | ScoreAnswer] = {
        question_id: _default_answer(question)
        for question_id, question in expected.items()
        if question_id not in exclude
    }
    answers.update(overrides or {})
    return make_answers(**answers)


def make_hunk_state(
    path: str = 'src/pkg/mod.py',
    header: str = '@@ -10,3 +10,5 @@ def do_thing',
    language: str = 'python',
    is_test: bool = False,
) -> HunkState:
    return {
        'task': None,
        'file': {'path': path, 'language': language, 'is_test': is_test, 'is_new': False},
        'hunk': {'header': header, 'diff': '', 'after': ''},
        'neighbours': {'same_module_helpers': [], 'tests_touching_file': []},
        'conventions': '',
    }


def make_hunk(
    overrides: dict[str, NoulAnswer | ScoreAnswer] | None = None,
    exclude: frozenset[str] = frozenset(),
    path: str = 'src/pkg/mod.py',
    header: str = '@@ -10,3 +10,5 @@ def do_thing',
) -> tuple[HunkState, SystemOneResponse]:
    """A `(HunkState, Answers)` pair, fully answered except for `overrides`/`exclude`."""
    state = make_hunk_state(path=path, header=header)
    return state, full_hunk_answers(state, overrides, exclude)


def make_task(n_criteria: int = 1) -> Task:
    return Task(
        id='T-99',
        title='Example task',
        acceptance=[f'criterion number {n}' for n in range(1, n_criteria + 1)],
        criteria_text='criteria text',
    )


def make_context(**overrides: object) -> Context:
    defaults: dict[str, object] = {
        'task': make_task(),
        'red_sha': 'deadbeef',
        'src_diff': 'some src diff',
        'test_diff': '',
    }
    defaults.update(overrides)
    return Context(**defaults)  # type: ignore[arg-type]


def make_check_report(
    findings: list[CheckFinding] | None = None,
    results: list[CheckResult] | None = None,
) -> CheckReport:
    if results is None:
        results = [
            CheckResult('red proof', 'pass'),
            CheckResult('green at HEAD', 'pass'),
            CheckResult('make check', 'pass'),
        ]
    return CheckReport(results=results, findings=findings or [])


def run(
    *,
    check_report: CheckReport | None = None,
    hunk_answers: list[tuple[HunkState, SystemOneResponse]] | None = None,
    change_answers: SystemOneResponse | None = None,
    context: Context | None = None,
) -> Review:
    resolved_context = context if context is not None else make_context()
    return compose(
        check_report=check_report if check_report is not None else make_check_report(),
        hunk_answers=hunk_answers if hunk_answers is not None else [],
        change_answers=change_answers if change_answers is not None else full_change_answers(resolved_context.task),
        context=resolved_context,
    )


# ---------------------------------------------------------------------------
# Verdict rule (§6.4): every row, with NEEDS_HUMAN precedence over
# CHANGES_REQUESTED over APPROVE.
# ---------------------------------------------------------------------------


def test_verdict_approve_when_nothing_fires() -> None:
    review = run()
    assert review.verdict == Verdict.APPROVE


def test_verdict_changes_requested_on_important_finding() -> None:
    review = run(hunk_answers=[make_hunk(overrides={'swallows_exception': noul(0.8)})])
    assert review.verdict == Verdict.CHANGES_REQUESTED


def test_verdict_needs_human_on_acceptance_tests_edited() -> None:
    check_report = make_check_report(findings=[CheckFinding('acceptance_tests_edited', 'tests/test_foo.py', 'diff')])
    review = run(check_report=check_report)
    assert review.verdict == Verdict.NEEDS_HUMAN


def test_verdict_needs_human_on_grey_zone_criterion() -> None:
    task = make_task()
    change_answers = full_change_answers(task, overrides={'criterion_1_satisfied': noul(0.5)})
    review = run(context=make_context(task=task), change_answers=change_answers)
    assert review.verdict == Verdict.NEEDS_HUMAN


def test_verdict_needs_human_on_missing_context() -> None:
    review = run(context=make_context(task=None))
    assert review.verdict == Verdict.NEEDS_HUMAN


def test_verdict_needs_human_precedence_over_blocker() -> None:
    """A blocker finding plus a grey-zone criterion still yields NEEDS_HUMAN."""
    task = make_task()
    change_answers = full_change_answers(task, overrides={'criterion_1_satisfied': noul(0.5)})
    review = run(
        context=make_context(task=task),
        hunk_answers=[make_hunk(overrides={'success_on_unverified': noul(0.9)})],
        change_answers=change_answers,
    )
    assert review.verdict == Verdict.NEEDS_HUMAN


# ---------------------------------------------------------------------------
# Score rule (§6.5): every row.
# ---------------------------------------------------------------------------


def test_score_1_on_success_on_unverified() -> None:
    review = run(hunk_answers=[make_hunk(overrides={'success_on_unverified': noul(0.9)})])
    assert review.score == 1


def test_score_2_on_blocker_finding() -> None:
    review = run(hunk_answers=[make_hunk(overrides={'indistinguishable_default': noul(0.9)})])
    assert review.score == 2


def test_score_2_on_failed_check() -> None:
    check_report = make_check_report(
        results=[
            CheckResult('red proof', 'pass'),
            CheckResult('green at HEAD', 'fail', 'boom'),
            CheckResult('make check', 'not_run'),
        ],
        findings=[CheckFinding('gate_failed', None, 'boom')],
    )
    review = run(check_report=check_report)
    assert review.score == 2


def test_score_3_on_important_finding() -> None:
    review = run(hunk_answers=[make_hunk(overrides={'swallows_exception': noul(0.8)})])
    assert review.score == 3


def test_score_4_on_only_minor_finding() -> None:
    review = run(hunk_answers=[make_hunk(overrides={'magic_constant': noul(0.8)})])
    assert review.score == 4


def test_score_5_on_no_findings() -> None:
    review = run()
    assert review.score == 5


# ---------------------------------------------------------------------------
# Threshold and gate mechanics (§6.1).
# ---------------------------------------------------------------------------


def test_noul_fires_at_threshold_not_just_under() -> None:
    at_threshold = run(hunk_answers=[make_hunk(overrides={'swallows_exception': noul(0.7)})])
    just_under = run(hunk_answers=[make_hunk(overrides={'swallows_exception': noul(0.699)})])

    assert [f.question_id for f in at_threshold.findings] == ['swallows_exception']
    assert just_under.findings == []


def test_score_over_threshold_low_confidence_is_uncertain_not_finding() -> None:
    review = run(hunk_answers=[make_hunk(overrides={'mixed_responsibility': score(1.6, 0.5)})])

    assert review.findings == []
    assert [f.question_id for f in review.uncertain] == ['mixed_responsibility']


def test_new_behaviour_untested_le_direction_fires_at_boundary() -> None:
    task = make_task()
    fires = run(
        context=make_context(task=task),
        change_answers=full_change_answers(task, overrides={'new_behaviour_untested': score(1.0, 0.8)}),
    )
    does_not_fire = run(
        context=make_context(task=task),
        change_answers=full_change_answers(task, overrides={'new_behaviour_untested': score(2.0, 0.8)}),
    )

    assert [f.question_id for f in fires.findings] == ['new_behaviour_untested']
    assert does_not_fire.findings == []


# ---------------------------------------------------------------------------
# Severity modifiers and dedupe (§5.5, §6.2, §6.3).
# ---------------------------------------------------------------------------


def test_touches_high_risk_promotes_new_behaviour_untested_to_blocker() -> None:
    task = make_task()
    promoted = run(
        context=make_context(task=task),
        change_answers=full_change_answers(
            task,
            overrides={'new_behaviour_untested': score(0.5, 0.8), 'touches_high_risk': noul(0.9)},
        ),
    )
    not_promoted = run(
        context=make_context(task=task),
        change_answers=full_change_answers(task, overrides={'new_behaviour_untested': score(0.5, 0.8)}),
    )

    [promoted_finding] = promoted.findings
    [plain_finding] = not_promoted.findings
    assert promoted_finding.severity == 'blocker'
    assert plain_finding.severity == 'important'


def test_dedupe_keeps_higher_probability() -> None:
    hunk_a = make_hunk_state(path='src/pkg/mod.py', header='@@ -1,1 +1,1 @@ def do_thing')
    hunk_b = make_hunk_state(path='src/pkg/mod.py', header='@@ -20,1 +20,1 @@ def do_thing')
    hunk_answers = [
        (hunk_a, full_hunk_answers(hunk_a, overrides={'swallows_exception': noul(0.8)})),
        (hunk_b, full_hunk_answers(hunk_b, overrides={'swallows_exception': noul(0.95)})),
    ]
    review = run(hunk_answers=hunk_answers)

    matching = [f for f in review.findings if f.question_id == 'swallows_exception']
    assert len(matching) == 1
    assert matching[0].probability == 0.95


def test_dedupe_key_includes_symbol_or_area() -> None:
    """Same id and file, different symbols, do NOT collapse into one finding."""
    hunk_foo = make_hunk_state(path='src/pkg/mod.py', header='@@ -1,1 +1,1 @@ def foo')
    hunk_bar = make_hunk_state(path='src/pkg/mod.py', header='@@ -1,1 +1,1 @@ def bar')
    hunk_answers = [
        (hunk_foo, full_hunk_answers(hunk_foo, overrides={'swallows_exception': noul(0.8)})),
        (hunk_bar, full_hunk_answers(hunk_bar, overrides={'swallows_exception': noul(0.9)})),
    ]
    review = run(hunk_answers=hunk_answers)

    matching = [f for f in review.findings if f.question_id == 'swallows_exception']
    assert len(matching) == 2
    assert {f.symbol_or_area for f in matching} == {'foo', 'bar'}


# ---------------------------------------------------------------------------
# Deterministic-check findings (§4.1, §7).
# ---------------------------------------------------------------------------


def test_check_finding_gets_severity_from_catalog() -> None:
    check_report = make_check_report(findings=[CheckFinding('gate_failed', None, 'make check failed')])
    review = run(check_report=check_report)

    [finding] = [f for f in review.findings if f.question_id == 'gate_failed']
    assert finding.severity == 'blocker'
    assert finding.confidence is None
    assert finding.probability is None


# ---------------------------------------------------------------------------
# Missing context (§6.6).
# ---------------------------------------------------------------------------


def test_missing_task_yields_missing_context_but_keeps_hunk_findings() -> None:
    context = make_context(task=None)
    review = run(
        hunk_answers=[make_hunk(overrides={'swallows_exception': noul(0.8)})],
        context=context,
        change_answers=full_change_answers(context.task),
    )

    assert review.stop_reason == 'missing_context'
    assert review.verdict == Verdict.NEEDS_HUMAN
    assert [f.question_id for f in review.findings] == ['swallows_exception']


def test_missing_context_via_red_sha_alone() -> None:
    review = run(context=make_context(red_sha=None))
    assert review.verdict == Verdict.NEEDS_HUMAN
    assert review.stop_reason == 'missing_context'


def test_missing_context_via_empty_diffs_alone() -> None:
    review = run(context=make_context(src_diff='', test_diff=''))
    assert review.verdict == Verdict.NEEDS_HUMAN
    assert review.stop_reason == 'missing_context'


# ---------------------------------------------------------------------------
# Counts (§7).
# ---------------------------------------------------------------------------


def test_counts_equal_findings_by_severity() -> None:
    hunk_answers = [
        make_hunk(path='a.py', header='@@ -1,1 +1,1 @@ def a', overrides={'indistinguishable_default': noul(0.9)}),
        make_hunk(path='b.py', header='@@ -1,1 +1,1 @@ def b', overrides={'swallows_exception': noul(0.9)}),
        make_hunk(path='c.py', header='@@ -1,1 +1,1 @@ def c', overrides={'duplicates_helper': noul(0.9)}),
        make_hunk(path='d.py', header='@@ -1,1 +1,1 @@ def d', overrides={'magic_constant': noul(0.9)}),
    ]
    review = run(hunk_answers=hunk_answers)

    by_severity: dict[str, int] = {'blocker': 0, 'important': 0, 'minor': 0, 'nit': 0}
    for finding in review.findings:
        by_severity[finding.severity] += 1

    assert review.counts == by_severity
    assert review.counts == {'blocker': 1, 'important': 2, 'minor': 1, 'nit': 0}


def test_counts_reflect_deduped_findings_not_raw() -> None:
    hunk_a = make_hunk_state(path='src/pkg/mod.py', header='@@ -1,1 +1,1 @@ def do_thing')
    hunk_b = make_hunk_state(path='src/pkg/mod.py', header='@@ -20,1 +20,1 @@ def do_thing')
    hunk_answers = [
        (hunk_a, full_hunk_answers(hunk_a, overrides={'swallows_exception': noul(0.8)})),
        (hunk_b, full_hunk_answers(hunk_b, overrides={'swallows_exception': noul(0.95)})),
    ]
    review = run(hunk_answers=hunk_answers)

    assert review.counts['important'] == 1


# ---------------------------------------------------------------------------
# Unanswered questions (fix round 1, item 1): compose derives the expected id set
# itself and treats a gap as `uncertain` + NEEDS_HUMAN, same precedence as
# missing_context. Findings from answered questions are kept.
# ---------------------------------------------------------------------------


def test_empty_answers_with_full_context_yields_needs_human_not_approve() -> None:
    hunk_state = make_hunk_state()
    review = run(hunk_answers=[(hunk_state, make_answers())])

    assert review.verdict == Verdict.NEEDS_HUMAN
    assert review.stop_reason == 'unanswered_questions'


def test_one_missing_question_on_one_hunk_flags_only_that_id() -> None:
    hunk_state = make_hunk_state()
    answers = full_hunk_answers(hunk_state, exclude=frozenset({'success_on_unverified'}))
    review = run(hunk_answers=[(hunk_state, answers)])

    assert review.verdict == Verdict.NEEDS_HUMAN
    assert review.stop_reason == 'unanswered_questions'
    unanswered_ids = [f.question_id for f in review.uncertain if f.reason == UNANSWERED]
    assert unanswered_ids == ['success_on_unverified']


def test_fully_answered_hunk_is_unaffected_by_the_unanswered_gate() -> None:
    hunk_state = make_hunk_state()
    answers = full_hunk_answers(hunk_state)
    review = run(hunk_answers=[(hunk_state, answers)])

    assert review.stop_reason != 'unanswered_questions'
    assert review.verdict == Verdict.APPROVE
    assert all(f.reason != UNANSWERED for f in review.uncertain)


# ---------------------------------------------------------------------------
# Unanswered gate, independent-pin edges (fix round 2, item 2). Answers are built
# from the hand-transcribed `_SPEC_*_IDS` sets above, never from `questions_for`,
# so a bug in compose.py's own id-set computation (wrong scope filter, dropped
# `criterion_questions`) cannot agree with the test by construction.
# ---------------------------------------------------------------------------


def test_python_test_hunk_missing_patches_unit_under_test_is_needs_human() -> None:
    hunk_state = make_hunk_state(language='python', is_test=True)
    ids = _SPEC_PYTHON_TEST_HUNK_IDS - {'patches_unit_under_test'}
    answers = _answers_for_ids(ids)
    review = run(hunk_answers=[(hunk_state, answers)])

    assert review.verdict == Verdict.NEEDS_HUMAN
    assert review.stop_reason == 'unanswered_questions'
    unanswered_ids = {f.question_id for f in review.uncertain if f.reason == UNANSWERED}
    assert 'patches_unit_under_test' in unanswered_ids


def test_non_python_hunk_missing_a_python_only_id_is_not_flagged() -> None:
    """A `hunk_python`-only id was never asked of a non-python hunk, so its absence
    must never be treated as unanswered.
    """
    hunk_state = make_hunk_state(language='rust', is_test=False)
    answers = _answers_for_ids(_SPEC_NON_PYTHON_HUNK_IDS)
    review = run(hunk_answers=[(hunk_state, answers)])

    unanswered_ids = {f.question_id for f in review.uncertain if f.reason == UNANSWERED}
    assert 'class_is_a_function' not in unanswered_ids
    assert review.stop_reason != 'unanswered_questions'
    assert review.verdict == Verdict.APPROVE


def test_change_missing_criterion_satisfied_is_needs_human() -> None:
    task = make_task()
    change_ids = _SPEC_5_4_AND_5_5_CHANGE_IDS | {'criterion_1_tested'}  # criterion_1_satisfied omitted on purpose
    answers = _answers_for_ids(change_ids, extra_catalog=criterion_questions(task))
    review = run(context=make_context(task=task), change_answers=answers)

    assert review.verdict == Verdict.NEEDS_HUMAN
    assert review.stop_reason == 'unanswered_questions'
    unanswered_ids = {f.question_id for f in review.uncertain if f.reason == UNANSWERED}
    assert 'criterion_1_satisfied' in unanswered_ids


def test_pinned_spec_id_sets_match_what_compose_expects() -> None:
    """Cross-check only: the hand-transcribed sets above still agree with
    `questions_for`. Unlike (a)-(c), this test is allowed to call `questions_for` --
    it exists to catch spec/catalog drift, not to gate compose.py's own logic.
    """
    assert set(questions_for('hunk', language='python', is_test=False)) == _SPEC_PYTHON_NON_TEST_HUNK_IDS
    assert set(questions_for('hunk', language='python', is_test=True)) == _SPEC_PYTHON_TEST_HUNK_IDS
    assert set(questions_for('hunk', language='rust', is_test=False)) == _SPEC_NON_PYTHON_HUNK_IDS
    assert set(questions_for('change')) == _SPEC_5_4_AND_5_5_CHANGE_IDS


# ---------------------------------------------------------------------------
# Unsupported primitive (fix round 1, item 2): a `Choice` question in the expected
# set is a bug in this file, not a review outcome.
# ---------------------------------------------------------------------------


def test_unsupported_choice_primitive_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    bogus = Question(
        id='bogus_choice',
        primitive=Choice(criteria={'a': None, 'b': None}, instructions='pick one'),
        scope='change',
        severity='minor',
        threshold=0.5,
        direction='ge',
        fix='fix {symbol} {path}',
        issue='issue',
        why='why',
        rubric='0',
    )
    real_questions_for = questions_module.questions_for

    def fake_questions_for(scope: Scope, language: str | None = None, is_test: bool = False) -> dict[str, Question]:
        result = dict(real_questions_for(scope, language=language, is_test=is_test))
        if scope == 'change':
            result['bogus_choice'] = bogus
        return result

    # `compose.py` no longer imports `questions_for` itself (fix round 1, item 3):
    # `expected_change_questions` in `questions.py` calls it internally, so that is
    # where the fake has to live for `compose.run()`'s change-scope expected set to
    # pick it up.
    monkeypatch.setattr(questions_module, 'questions_for', fake_questions_for)

    with pytest.raises(ComposeInvariantError, match='bogus_choice'):
        run()


# ---------------------------------------------------------------------------
# Pinned mutant-killing rows (fix round 1, item 3).
# ---------------------------------------------------------------------------


def test_score_confidence_equal_to_minimum_fires_as_finding() -> None:
    """(a) confidence == confidence_min fires; it is not the `<` case."""
    review = run(hunk_answers=[make_hunk(overrides={'mixed_responsibility': score(1.6, 0.6)})])

    assert [f.question_id for f in review.findings if f.question_id == 'mixed_responsibility'] == [
        'mixed_responsibility'
    ]
    assert all(f.question_id != 'mixed_responsibility' for f in review.uncertain)


@pytest.mark.parametrize(
    ('value', 'expect_finding', 'expect_grey'),
    [
        (0.4, True, False),  # (c) exactly the catalog threshold: fires as a finding
        (0.69, False, True),  # just inside the grey zone
        (0.7, False, False),  # the named upper bound itself: satisfied, not grey
    ],
)
def test_grey_zone_boundaries_for_criterion_satisfied(value: float, expect_finding: bool, expect_grey: bool) -> None:
    task = make_task()
    change_answers = full_change_answers(task, overrides={'criterion_1_satisfied': noul(value)})
    review = run(context=make_context(task=task), change_answers=change_answers)

    found = any(f.question_id == 'criterion_1_satisfied' for f in review.findings)
    grey = any(f.question_id == 'criterion_1_satisfied' and f.reason is None for f in review.uncertain)
    assert found is expect_finding
    assert grey is expect_grey


def test_failed_check_result_without_a_finding_still_caps_score_at_2() -> None:
    """(d) `any_check_failed` alone, with no `CheckFinding` row, still forces <= 2.

    `CheckReport` is a plain dataclass with independent `results`/`findings` lists,
    so this combination -- one a real `checks.py` run never produces on its own, since
    every failing result there also appends a matching finding -- is directly
    constructible and is exactly what `compose.py`'s own `any_check_failed` flag
    (read from `results`, not `findings`) exists to cover.
    """
    check_report = CheckReport(results=[CheckResult('some check', 'fail', 'boom')], findings=[])
    review = run(check_report=check_report)
    assert review.score <= 2


def test_counts_reflect_deduped_findings_only_once() -> None:
    """(f) alias of `test_counts_reflect_deduped_findings_not_raw`, phrased against
    `counts` directly rather than `findings`, per the fix-round item wording."""
    hunk_a = make_hunk_state(path='src/pkg/mod.py', header='@@ -1,1 +1,1 @@ def do_thing')
    hunk_b = make_hunk_state(path='src/pkg/mod.py', header='@@ -20,1 +20,1 @@ def do_thing')
    hunk_answers = [
        (hunk_a, full_hunk_answers(hunk_a, overrides={'duplicates_helper': noul(0.9)})),
        (hunk_b, full_hunk_answers(hunk_b, overrides={'duplicates_helper': noul(0.95)})),
    ]
    review = run(hunk_answers=hunk_answers)
    assert review.counts == {'blocker': 0, 'important': 1, 'minor': 0, 'nit': 0}


def test_compose_calls_check_hard_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    """(g) `compose()` itself invokes the hard-constraint guard."""

    def boom(*args: object, **kwargs: object) -> None:
        raise ComposeInvariantError('boom from monkeypatch')

    monkeypatch.setattr(compose_module, 'check_hard_constraints', boom)

    with pytest.raises(ComposeInvariantError, match='boom from monkeypatch'):
        run()


def test_summary_differs_between_verdicts_and_mentions_counts() -> None:
    """(h) `summary` differs by verdict and names the counts for CHANGES_REQUESTED."""
    approve_review = run()
    changes_requested_review = run(hunk_answers=[make_hunk(overrides={'swallows_exception': noul(0.8)})])

    assert approve_review.summary != changes_requested_review.summary
    assert str(changes_requested_review.counts['important']) in changes_requested_review.summary


# ---------------------------------------------------------------------------
# Pinned mutant-killing rows (fix round 2, item 3).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(('value', 'expect_blocker'), [(0.6, True), (0.59, False)])
def test_touches_high_risk_threshold_boundary(value: float, expect_blocker: bool) -> None:
    task = make_task()
    change_answers = full_change_answers(
        task,
        overrides={'new_behaviour_untested': score(0.5, 0.8), 'touches_high_risk': noul(value)},
    )
    review = run(context=make_context(task=task), change_answers=change_answers)

    [finding] = review.findings
    assert (finding.severity == 'blocker') is expect_blocker


def test_check_finding_severity_is_looked_up_live_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `CheckFinding`'s severity comes from a live catalog lookup by id, not a
    hardcoded `'blocker'`: patching the catalog's own severity for `gate_failed`
    changes what `compose()` reports.
    """
    patched_catalog = tuple(replace(q, severity='minor') if q.id == 'gate_failed' else q for q in CATALOG)
    monkeypatch.setattr(compose_module, 'CATALOG', patched_catalog)

    check_report = make_check_report(findings=[CheckFinding('gate_failed', None, 'boom')])
    review = run(check_report=check_report)

    [finding] = [f for f in review.findings if f.question_id == 'gate_failed']
    assert finding.severity == 'minor'


# ---------------------------------------------------------------------------
# Hard constraints (reviewer.md scoring section).
# ---------------------------------------------------------------------------


def _finding(severity: Severity, question_id: str) -> Finding:
    return Finding(
        severity=severity,
        file=None,
        symbol_or_area='<module>',
        issue='issue',
        why_it_matters='why',
        concrete_fix='fix',
        blocks_merge=severity in ('blocker', 'important'),
        question_id=question_id,
        probability=0.9,
        confidence=None,
    )


@pytest.mark.parametrize(
    ('verdict', 'score', 'findings', 'any_check_failed'),
    [
        (Verdict.APPROVE, 4, [_finding('blocker', 'success_on_unverified')], False),
        (Verdict.APPROVE, 4, [_finding('important', 'swallows_exception')], False),
        (Verdict.CHANGES_REQUESTED, 5, [], True),
    ],
)
def test_hard_constraint_raises_on_impossible_input(
    verdict: Verdict, score: int, findings: list[Finding], any_check_failed: bool
) -> None:
    with pytest.raises(ComposeInvariantError):
        check_hard_constraints(verdict, score, findings, any_check_failed=any_check_failed)


def test_hard_constraint_does_not_raise_on_consistent_input() -> None:
    check_hard_constraints(Verdict.APPROVE, 4, [], any_check_failed=False)
