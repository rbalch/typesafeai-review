"""Acceptance tests for T-06: question catalog with severity, threshold and fix
templates (spec §5, §4.0, §7).
"""

from __future__ import annotations

import re

import msgspec
import pytest
from typesafe_sdk import Noul, Score

from typesafe_review.questions import CATALOG, Question, criterion_questions, questions_for
from typesafe_review.taskfile import Task

# The static ids from spec §5.1-§5.5, minus the per-criterion pair (built by
# `criterion_questions`, not part of `CATALOG`).
_SPEC_5_1 = {
    'swallows_exception',
    'bare_except',
    'success_on_unverified',
    'indistinguishable_default',
    'auth_passes_on_error',
    'credential_in_output',
}
_SPEC_5_2 = {
    'duplicates_helper',
    'violates_convention',
    'magic_constant',
    'vague_error_message',
    'mixed_responsibility',
}
_SPEC_5_3 = {
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
_SPEC_5_4 = {'tests_fitted_to_code', 'scope_creep'}
_SPEC_5_5 = {
    'new_behaviour_untested',
    'touches_high_risk',
    'patches_unit_under_test',
    'mock_hides_integration',
    'unittest_testcase_style',
}
_ALL_STATIC_IDS = _SPEC_5_1 | _SPEC_5_2 | _SPEC_5_3 | _SPEC_5_4 | _SPEC_5_5

_CHECK_IDS = {'red_proof_missing', 'acceptance_tests_edited', 'gate_failed'}

_BACKTICK_PATH_RE = re.compile(r'`[A-Za-z_][\w.]*`')


def _flatten_text(value: object) -> str:
    """Flatten a structured instructions/criteria value into one search string."""
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ' '.join(_flatten_text(v) for v in value.values())
    if isinstance(value, list | tuple):
        return ' '.join(_flatten_text(v) for v in value)
    return str(value)


def _is_noul(question: Question) -> bool:
    return isinstance(question.primitive, Noul)


def _is_score(question: Question) -> bool:
    return isinstance(question.primitive, Score)


def test_every_id_is_unique():
    ids = [q.id for q in CATALOG]
    assert len(ids) == len(set(ids))


def test_every_static_spec_id_present_in_catalog():
    catalog_ids = {q.id for q in CATALOG}
    assert _ALL_STATIC_IDS <= catalog_ids


def test_check_ids_present_with_no_primitive():
    by_id = {q.id: q for q in CATALOG}
    assert _CHECK_IDS <= set(by_id)
    for check_id in _CHECK_IDS:
        assert by_id[check_id].primitive is None
        assert by_id[check_id].scope == 'check'


def test_every_noul_instruction_references_a_backticked_state_path():
    nouls = [q for q in CATALOG if _is_noul(q)]
    assert nouls, 'expected at least one Noul question in the catalog'
    for q in nouls:
        assert isinstance(q.primitive, Noul)
        text = _flatten_text(q.primitive.instructions)
        assert _BACKTICK_PATH_RE.search(text), f'{q.id}: instructions have no backticked state path: {text!r}'


def test_every_score_has_two_to_ten_levels():
    scores = [q for q in CATALOG if _is_score(q)]
    assert scores, 'expected at least one Score question in the catalog'
    for q in scores:
        assert isinstance(q.primitive, Score)
        levels = q.primitive.criteria
        assert 2 <= len(levels) <= 10, f'{q.id}: has {len(levels)} levels'


def test_new_behaviour_untested_fires_low_every_other_catalog_question_fires_high():
    by_id = {q.id: q for q in CATALOG}
    assert by_id['new_behaviour_untested'].direction == 'le'
    for q in CATALOG:
        if q.id == 'new_behaviour_untested':
            continue
        assert q.direction == 'ge', f'{q.id}: expected direction "ge", got {q.direction!r}'


def test_criterion_questions_both_directions_are_le():
    task = Task(id='T-99', title='x', acceptance=['first thing', 'second thing'], criteria_text='')
    questions = criterion_questions(task)
    for qid, q in questions.items():
        assert q.direction == 'le', f'{qid}: expected direction "le", got {q.direction!r}'


def test_every_fix_formats_with_symbol_and_path_and_no_other_keys():
    task = Task(id='T-99', title='x', acceptance=['first thing'], criteria_text='')
    all_questions = list(CATALOG) + list(criterion_questions(task).values())
    for q in all_questions:
        formatted = q.fix.format(symbol='resolve_bind_address', path='src/pkg/config.py')
        assert '{' not in formatted and '}' not in formatted, f'{q.id}: fix left unformatted braces: {formatted!r}'


def test_questions_for_python_non_test_hunk_gets_hunk_and_hunk_python():
    result = questions_for('hunk', language='python', is_test=False)
    assert 'swallows_exception' in result  # hunk
    assert 'class_is_a_function' in result  # hunk_python
    assert all(q.scope in ('hunk', 'hunk_python') for q in result.values())
    assert 'patches_unit_under_test' not in result  # hunk_test excluded


def test_questions_for_test_hunk_gets_hunk_and_hunk_test():
    result = questions_for('hunk', language='python', is_test=True)
    assert 'swallows_exception' in result  # hunk
    assert 'patches_unit_under_test' in result  # hunk_test
    assert all(q.scope in ('hunk', 'hunk_test') for q in result.values())
    assert 'class_is_a_function' not in result  # hunk_python excluded


def test_questions_for_other_language_gets_hunk_only():
    result = questions_for('hunk', language='javascript', is_test=False)
    assert 'swallows_exception' in result
    assert all(q.scope == 'hunk' for q in result.values())
    assert 'class_is_a_function' not in result
    assert 'patches_unit_under_test' not in result


def test_questions_for_change_scope_returns_change_questions_only():
    result = questions_for('change')
    assert all(q.scope == 'change' for q in result.values())
    assert 'tests_fitted_to_code' in result
    assert 'new_behaviour_untested' in result


def test_criterion_questions_on_two_criterion_task_yields_four_questions_with_criterion_text():
    task = Task(
        id='T-99',
        title='x',
        acceptance=['covers the create path', 'covers the delete path'],
        criteria_text='',
    )
    questions = criterion_questions(task)
    assert set(questions) == {
        'criterion_1_satisfied',
        'criterion_1_tested',
        'criterion_2_satisfied',
        'criterion_2_tested',
    }
    criterion_1_satisfied = questions['criterion_1_satisfied'].primitive
    criterion_1_tested = questions['criterion_1_tested'].primitive
    criterion_2_satisfied = questions['criterion_2_satisfied'].primitive
    criterion_2_tested = questions['criterion_2_tested'].primitive
    assert isinstance(criterion_1_satisfied, Noul)
    assert isinstance(criterion_1_tested, Noul)
    assert isinstance(criterion_2_satisfied, Noul)
    assert isinstance(criterion_2_tested, Noul)
    assert 'covers the create path' in _flatten_text(criterion_1_satisfied.instructions)
    assert 'covers the create path' in _flatten_text(criterion_1_tested.instructions)
    assert 'covers the delete path' in _flatten_text(criterion_2_satisfied.instructions)
    assert 'covers the delete path' in _flatten_text(criterion_2_tested.instructions)


# id -> (severity, threshold, direction), transcribed by hand from
# docs/specs/typesafe-reviewer.md §5.1, §5.2, §5.4, §5.5 and §4.1 -- never imported
# from `CATALOG`, so a change to the catalog's own data cannot also change what this
# test expects.
_SPEC_SEVERITY_THRESHOLD_DIRECTION: dict[str, tuple[str, float, str]] = {
    # §5.1 Failure direction.
    'swallows_exception': ('important', 0.7, 'ge'),
    'bare_except': ('important', 0.7, 'ge'),
    'success_on_unverified': ('blocker', 0.6, 'ge'),
    'indistinguishable_default': ('blocker', 0.6, 'ge'),
    'auth_passes_on_error': ('blocker', 0.5, 'ge'),
    'credential_in_output': ('blocker', 0.5, 'ge'),
    # §5.2 Correctness and fit.
    'duplicates_helper': ('important', 0.7, 'ge'),
    'violates_convention': ('important', 0.7, 'ge'),
    'magic_constant': ('minor', 0.75, 'ge'),
    'vague_error_message': ('minor', 0.75, 'ge'),
    'mixed_responsibility': ('important', 1.5, 'ge'),
    # §5.4 Task satisfaction (the two non-per-criterion rows).
    'tests_fitted_to_code': ('blocker', 0.6, 'ge'),
    'scope_creep': ('important', 1.5, 'ge'),
    # §5.5 Test adequacy.
    'new_behaviour_untested': ('important', 1.0, 'le'),
    'touches_high_risk': ('modifier', 0.6, 'ge'),
    'patches_unit_under_test': ('important', 0.7, 'ge'),
    'mock_hides_integration': ('minor', 0.75, 'ge'),
    'unittest_testcase_style': ('nit', 0.8, 'ge'),
    # §4.1 Deterministic-check ids.
    'red_proof_missing': ('blocker', 1.0, 'ge'),
    'acceptance_tests_edited': ('blocker', 1.0, 'ge'),
    'gate_failed': ('blocker', 1.0, 'ge'),
}


def test_severity_threshold_direction_pinned_to_spec():
    by_id = {q.id: q for q in CATALOG}
    for qid, (severity, threshold, direction) in _SPEC_SEVERITY_THRESHOLD_DIRECTION.items():
        q = by_id[qid]
        assert (q.severity, q.threshold, q.direction) == (severity, threshold, direction), (
            f'{qid}: expected {(severity, threshold, direction)}, got {(q.severity, q.threshold, q.direction)}'
        )


def test_criterion_questions_severity_threshold_direction_pinned_to_spec():
    task = Task(id='T-99', title='x', acceptance=['first thing', 'second thing'], criteria_text='')
    questions = criterion_questions(task)
    for n in (1, 2):
        satisfied = questions[f'criterion_{n}_satisfied']
        assert (satisfied.severity, satisfied.threshold, satisfied.direction) == ('blocker', 0.4, 'le')
        tested = questions[f'criterion_{n}_tested']
        assert (tested.severity, tested.threshold, tested.direction) == ('important', 0.4, 'le')


def test_hunk_python_nouls_have_nonempty_distinct_true_false_criteria():
    hunk_python_nouls = [q for q in CATALOG if q.scope == 'hunk_python']
    assert hunk_python_nouls, 'expected at least one hunk_python question in the catalog'
    for q in hunk_python_nouls:
        assert isinstance(q.primitive, Noul), f'{q.id}: expected a Noul for scope hunk_python'
        criteria = q.primitive.criteria
        assert criteria is not None, f'{q.id}: expected non-None criteria for a §5.3 question'
        true_text = criteria.get('true')
        false_text = criteria.get('false')
        assert isinstance(true_text, str) and true_text.strip(), f'{q.id}: criteria.true is empty'
        assert isinstance(false_text, str) and false_text.strip(), f'{q.id}: criteria.false is empty'
        instructions = q.primitive.instructions
        question_text = instructions['question'] if isinstance(instructions, dict) else instructions
        assert true_text != question_text, (
            f'{q.id}: criteria.true is a verbatim copy of the question, not an independent statement'
        )


def test_every_non_none_primitive_encodes_on_the_wire():
    task = Task(id='T-99', title='x', acceptance=['first thing'], criteria_text='')
    all_questions = list(CATALOG) + list(criterion_questions(task).values())
    for q in all_questions:
        if q.primitive is None:
            continue
        encoded = msgspec.json.encode(q.primitive)
        decoded = msgspec.json.decode(encoded)
        assert 'type' in decoded, f'{q.id}: encoded form has no "type" key: {decoded!r}'


if __name__ == '__main__':
    pytest.main([__file__, '-q'])
