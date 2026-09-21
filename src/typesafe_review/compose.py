"""Compose findings, verdict, score, `stop_reason` and `uncertain` from answers.

Spec §6, §7. Pure functions over plain data: this module never calls the model and
never talks to `cli.py`. Severity, verdict and score come only from the tables in
`questions.py` (imported) and the two named constants here -- never from anything the
model returned as text.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from typesafe_sdk import Choice, SystemOneResponse

from typesafe_review.checks import CheckFinding, CheckReport, CheckResult
from typesafe_review.questions import (
    CATALOG,
    Question,
    Severity,
    criterion_questions,
    expected_change_questions,
    expected_hunk_questions,
)
from typesafe_review.slicing import symbol_from_header
from typesafe_review.state import MAX_REQUEST_TOKENS, HunkState
from typesafe_review.taskfile import Task
from typesafe_review.verdict import Verdict

#: One request's worth of answers, T-07's per-request answer object.
Answers = SystemOneResponse

#: §6.5 grey-zone upper bound for `criterion_*_satisfied`. The lower bound is the
#: question's own 0.4 threshold in the catalog; only the upper bound is named here.
GREY_ZONE_UPPER = 0.7

#: `symbol_or_area` for a change-wide finding: there is no single hunk to name.
_CHANGE_AREA = '<change>'

#: `symbol_or_area` for a deterministic-check finding: it never names a symbol.
_CHECK_AREA = '<module>'

#: The two ids that always drop the composed score to 1, per reviewer.md/§6.5.
_SCORE_1_IDS = {'success_on_unverified', 'auth_passes_on_error'}

_SEVERITIES: tuple[Severity, ...] = ('blocker', 'important', 'minor', 'nit')

#: `reason` on an `uncertain` `Finding` for a question the catalog expected an
#: answer for but the response never included, for the hunk or change it belonged to.
UNANSWERED = 'unanswered'

#: `reason` on an `uncertain` `Finding`, and `Review.stop_reason`, for a request
#: `pipeline._build_requests` never sent because its estimate exceeded
#: `MAX_REQUEST_TOKENS` (RA-06). Sibling of `UNANSWERED`: a skipped request never got
#: a chance to answer at all, unlike an unanswered one the model actually saw.
STATE_TOO_LARGE = 'state_too_large'


class ComposeInvariantError(Exception):
    """A hard verdict/score constraint from `reviewer.md`'s scoring section was
    violated. This is always a bug in `compose.py`, never a review outcome.
    """


@dataclass(frozen=True)
class Context:
    """The pieces `compose` needs beyond the answers themselves, for §6.6.

    Duplicates `ChangeState`'s `task`/`src_diff`/`test_diff` on purpose for now: T-10
    builds one `Context` from a `ChangeState` plus the `red_sha` it already has, so
    the two shapes have a single source at the call site even though they overlap
    here.
    """

    task: Task | None
    red_sha: str | None
    src_diff: str
    test_diff: str


@dataclass(frozen=True)
class Finding:
    """`reviewer.md`'s finding fields, plus the model's own question id, probability
    and confidence. `confidence` is `None` for Nouls and for deterministic-check
    findings, which have no model answer at all (`probability` is `None` there too).
    `reason` is `None` for an ordinary fired or grey-zone finding, and `'unanswered'`
    for a question the catalog expected an answer for that the response never gave.
    """

    severity: Severity
    file: str | None
    symbol_or_area: str
    issue: str
    why_it_matters: str
    concrete_fix: str
    blocks_merge: bool
    question_id: str
    probability: float | None
    confidence: float | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Skipped:
    """One request `pipeline._build_requests` never sent because its estimate
    exceeded `budget` (RA-06). `render.py` lists these under `## Required Checks`;
    `render_json` carries them verbatim under `"skipped"`.
    """

    key: str
    estimated_tokens: int
    budget: int


@dataclass(frozen=True)
class Review:
    verdict: Verdict
    score: int
    summary: str
    findings: list[Finding]
    uncertain: list[Finding]
    counts: dict[str, int]
    stop_reason: str
    required_checks: list[CheckResult]
    #: RA-06. Defaults to `[]` so every pre-RA-06 caller that builds a `Review` by
    #: hand (fixture-style tests elsewhere in the suite) keeps working unchanged;
    #: `compose()` itself always passes this explicitly.
    skipped: list[Skipped] = field(default_factory=list)


def _catalog_lookup(task: Task | None) -> dict[str, Question]:
    lookup = {q.id: q for q in CATALOG}
    if task is not None:
        lookup.update(criterion_questions(task))
    return lookup


def _assert_supported_primitives(questions: dict[str, Question]) -> None:
    """`compose` only reads `.nouls` and `.scores`. A `Choice` question in the
    expected set is a catalog bug, not a review outcome the model can help with.
    """
    for question in questions.values():
        if isinstance(question.primitive, Choice):
            raise ComposeInvariantError(f'unsupported primitive Choice for {question.id!r}')


def _answered_ids(answers: Answers | None) -> set[str]:
    if answers is None:
        # RA-06: a skipped hunk/change never got a request at all, so nothing in
        # `expected` counts as answered -- every id is "missing".
        return set()
    return set(answers.nouls) | set(answers.scores) | set(answers.choices)


def _unanswered_findings(
    expected: dict[str, Question],
    answers: Answers | None,
    *,
    path: str | None,
    symbol: str,
    reason: str = UNANSWERED,
) -> list[Finding]:
    missing = expected.keys() - _answered_ids(answers)
    return [
        _render_finding(
            expected[question_id],
            severity=expected[question_id].severity,
            path=path,
            symbol=symbol,
            probability=None,
            confidence=None,
            reason=reason,
        )
        for question_id in missing
    ]


def _fires(question: Question, value: float) -> bool:
    if question.direction == 'ge':
        return value >= question.threshold
    return value <= question.threshold


def _is_criterion_satisfied(question_id: str) -> bool:
    return question_id.startswith('criterion_') and question_id.endswith('_satisfied')


def _in_grey_zone(question: Question, value: float) -> bool:
    return question.threshold < value < GREY_ZONE_UPPER


def _render_finding(
    question: Question,
    *,
    severity: Severity,
    path: str | None,
    symbol: str,
    probability: float | None,
    confidence: float | None,
    reason: str | None = None,
) -> Finding:
    fmt = {'symbol': symbol, 'path': path or ''}
    return Finding(
        severity=severity,
        file=path,
        symbol_or_area=symbol,
        issue=question.issue.format(**fmt),
        why_it_matters=question.why.format(**fmt),
        concrete_fix=question.fix.format(**fmt),
        blocks_merge=severity in ('blocker', 'important'),
        question_id=question.id,
        probability=probability,
        confidence=confidence,
        reason=reason,
    )


def _effective_severity(question: Question, *, high_risk: bool) -> Severity:
    if question.id == 'new_behaviour_untested' and high_risk:
        return 'blocker'
    return question.severity


def _process_answers(
    catalog: dict[str, Question],
    answers: Answers,
    *,
    path: str | None,
    symbol: str,
    high_risk: bool,
) -> tuple[list[Finding], list[Finding]]:
    """Turn one hunk's or the change's answers into (findings, uncertain).

    `high_risk` only ever changes anything for `new_behaviour_untested`, which is
    change-scoped; passing `False` for hunk answers is always a no-op.
    """
    findings: list[Finding] = []
    uncertain: list[Finding] = []

    for question_id, noul_answer in answers.nouls.items():
        question = catalog.get(question_id)
        if question is None or question.severity == 'modifier':
            continue
        value = noul_answer.noul
        if _is_criterion_satisfied(question_id) and _in_grey_zone(question, value):
            uncertain.append(
                _render_finding(
                    question, severity=question.severity, path=path, symbol=symbol, probability=value, confidence=None
                )
            )
            continue
        if _fires(question, value):
            severity = _effective_severity(question, high_risk=high_risk)
            findings.append(
                _render_finding(
                    question, severity=severity, path=path, symbol=symbol, probability=value, confidence=None
                )
            )

    for question_id, score_answer in answers.scores.items():
        question = catalog.get(question_id)
        if question is None or question.severity == 'modifier':
            continue
        value = score_answer.score
        if not _fires(question, value):
            continue
        severity = _effective_severity(question, high_risk=high_risk)
        confidence = score_answer.confidence
        target = findings
        if question.confidence_min is not None and confidence < question.confidence_min:
            target = uncertain
        target.append(
            _render_finding(
                question, severity=severity, path=path, symbol=symbol, probability=value, confidence=confidence
            )
        )

    return findings, uncertain


def _check_finding_to_finding(check_finding: CheckFinding, catalog: dict[str, Question]) -> Finding:
    question = catalog[check_finding.id]
    return _render_finding(
        question,
        severity=question.severity,
        path=check_finding.path,
        symbol=_CHECK_AREA,
        probability=None,
        confidence=None,
    )


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """§6.3: same id, file and symbol across hunks collapse to one finding, keeping
    the highest probability. Order is first-seen.
    """
    best: dict[tuple[str, str | None, str], Finding] = {}
    order: list[tuple[str, str | None, str]] = []
    for finding in findings:
        key = (finding.question_id, finding.file, finding.symbol_or_area)
        current = best.get(key)
        if key not in best:
            order.append(key)
        if current is None or (finding.probability or 0.0) > (current.probability or 0.0):
            best[key] = finding
    return [best[key] for key in order]


def _counts(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {severity: 0 for severity in _SEVERITIES}
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def _score_for(findings: list[Finding], *, any_check_failed: bool) -> int:
    if any(f.question_id in _SCORE_1_IDS or _is_criterion_satisfied(f.question_id) for f in findings):
        return 1
    if any(f.severity == 'blocker' for f in findings) or any_check_failed:
        return 2
    if any(f.severity == 'important' for f in findings):
        return 3
    if findings:
        return 4
    return 5


def _stop_reason(*, skipped_present: bool, missing_context: bool, unanswered_present: bool, verdict: Verdict) -> str:
    if skipped_present:
        return STATE_TOO_LARGE
    if missing_context:
        return 'missing_context'
    if unanswered_present:
        return 'unanswered_questions'
    if verdict is Verdict.APPROVE:
        return 'approved'
    if verdict is Verdict.CHANGES_REQUESTED:
        return 'changes_requested'
    return 'needs_human'


def _summary(verdict: Verdict, counts: dict[str, int]) -> str:
    if verdict is Verdict.APPROVE:
        return 'No blocking issues found; ready to merge.'
    if verdict is Verdict.NEEDS_HUMAN:
        return 'Human judgement is needed before this can proceed.'
    return (
        f'{counts["blocker"]} blocker(s) and {counts["important"]} important finding(s) must be resolved before merge.'
    )


def check_hard_constraints(
    verdict: Verdict,
    score: int,
    findings: list[Finding],
    *,
    any_check_failed: bool,
) -> None:
    """Raise `ComposeInvariantError` if `verdict`/`score` disagree with `findings` or
    `any_check_failed`, per `reviewer.md`'s scoring hard constraints.
    """
    has_blocker_or_important = any(f.severity in ('blocker', 'important') for f in findings)
    if verdict is Verdict.APPROVE and has_blocker_or_important:
        raise ComposeInvariantError('APPROVE with an open blocker or important finding')
    if score == 5 and any_check_failed:
        raise ComposeInvariantError('score 5 with a failed required check')


def compose(
    check_report: CheckReport,
    hunk_answers: list[tuple[HunkState, Answers | None]],
    change_answers: Answers | None,
    context: Context,
    *,
    skipped: Sequence[tuple[str, int]] = (),
) -> Review:
    """Spec §6 in code: answers in, a `Review` out. See module docstring.

    A hunk's or the change's `answers` is `None` exactly when `pipeline.run` never
    sent that request at all (RA-06: its estimate exceeded `MAX_REQUEST_TOKENS`,
    caught by `pipeline._build_requests` before `ask_all` ran) -- never a stand-in
    response. Every question that hunk or the change would have been asked becomes
    `uncertain` with `reason=STATE_TOO_LARGE` instead of going through
    `_process_answers`/`_unanswered_findings` at all. `skipped` is
    `pipeline._build_requests`'s own `(key, estimate)` list, carried through to
    `Review.skipped` for `render.py` -- it plays no part in deciding which hunk or
    the change was skipped; a `None` answer already says that directly.
    """
    catalog = _catalog_lookup(context.task)

    check_findings = [_check_finding_to_finding(cf, catalog) for cf in check_report.findings]

    skipped_present = change_answers is None or any(answers is None for _, answers in hunk_answers)

    hunk_findings: list[Finding] = []
    hunk_uncertain: list[Finding] = []
    unanswered_present = False
    for hunk_state, answers in hunk_answers:
        path = hunk_state['file']['path']
        symbol = symbol_from_header(hunk_state['hunk']['header'])
        expected = expected_hunk_questions(hunk_state['file']['language'], hunk_state['file']['is_test'])
        _assert_supported_primitives(expected)
        if answers is None:
            hunk_uncertain.extend(
                _unanswered_findings(expected, None, path=path, symbol=symbol, reason=STATE_TOO_LARGE)
            )
            continue
        findings, uncertain = _process_answers(catalog, answers, path=path, symbol=symbol, high_risk=False)
        hunk_findings.extend(findings)
        hunk_uncertain.extend(uncertain)
        missing = _unanswered_findings(expected, answers, path=path, symbol=symbol)
        if missing:
            unanswered_present = True
            hunk_uncertain.extend(missing)

    expected_change = expected_change_questions(context.task)
    _assert_supported_primitives(expected_change)

    if change_answers is None:
        change_findings: list[Finding] = []
        change_uncertain = _unanswered_findings(
            expected_change, None, path=None, symbol=_CHANGE_AREA, reason=STATE_TOO_LARGE
        )
    else:
        high_risk_question = catalog.get('touches_high_risk')
        high_risk_answer = change_answers.nouls.get('touches_high_risk')
        high_risk = (
            high_risk_question is not None
            and high_risk_answer is not None
            and _fires(high_risk_question, high_risk_answer.noul)
        )

        change_findings, change_uncertain = _process_answers(
            catalog, change_answers, path=None, symbol=_CHANGE_AREA, high_risk=high_risk
        )

        missing_change = _unanswered_findings(expected_change, change_answers, path=None, symbol=_CHANGE_AREA)
        if missing_change:
            unanswered_present = True
            change_uncertain.extend(missing_change)

    findings = _dedupe(check_findings + hunk_findings + change_findings)
    uncertain = _dedupe(hunk_uncertain + change_uncertain)

    missing_context = (
        context.task is None or context.red_sha is None or (not context.src_diff and not context.test_diff)
    )
    acceptance_tests_edited = any(f.question_id == 'acceptance_tests_edited' for f in check_findings)
    grey_zone_hit = any(_is_criterion_satisfied(f.question_id) for f in uncertain)

    if missing_context or unanswered_present or acceptance_tests_edited or grey_zone_hit or skipped_present:
        verdict = Verdict.NEEDS_HUMAN
    elif any(f.severity in ('blocker', 'important') for f in findings):
        verdict = Verdict.CHANGES_REQUESTED
    else:
        verdict = Verdict.APPROVE

    any_check_failed = any(result.status == 'fail' for result in check_report.results)
    score = _score_for(findings, any_check_failed=any_check_failed)
    counts = _counts(findings)
    stop_reason = _stop_reason(
        skipped_present=skipped_present,
        missing_context=missing_context,
        unanswered_present=unanswered_present,
        verdict=verdict,
    )
    summary = _summary(verdict, counts)

    check_hard_constraints(verdict, score, findings, any_check_failed=any_check_failed)

    review_skipped = [
        Skipped(key=key, estimated_tokens=estimate, budget=MAX_REQUEST_TOKENS) for key, estimate in skipped
    ]

    return Review(
        verdict=verdict,
        score=score,
        summary=summary,
        findings=findings,
        uncertain=uncertain,
        counts=counts,
        stop_reason=stop_reason,
        required_checks=list(check_report.results),
        skipped=review_skipped,
    )
