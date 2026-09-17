"""The question catalog: spec §5, as data.

Every question the reviewer can ask is a `Question` here: the SDK primitive that will
be sent to `system_one`, the scope it applies to, its severity, the threshold and
direction that turn an answer into a finding, and the templated fix/issue/why text.
Nothing in this module calls the API; `ask.py` (T-07) does that, and `compose.py`
(T-08) is the only place that turns a fired threshold into a finding.

Ids are stable and double as finding ids downstream. Never hardcode one as a string
outside this module -- iterate `CATALOG`, or look a `Question` up by the id a fired
answer already carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from typesafe_sdk import Choice, JSONContent, Noul, NoulCriteria, Score

from typesafe_review.taskfile import Task

Scope = Literal['hunk', 'hunk_python', 'hunk_test', 'change', 'check']
Severity = Literal['blocker', 'important', 'minor', 'nit', 'modifier']
Direction = Literal['ge', 'le']

#: Rubric section reference for the deterministic-check ids, which have no §5 row.
_CHECK_RUBRIC = '4.1'


@dataclass(frozen=True)
class Question:
    """One catalog entry: a question plus everything code needs to score its answer.

    `primitive` is `None` for the three deterministic-check ids (§4.1): they never go
    to the model, but severity and fix text still need one home, per T-06 item 7.
    `threshold`/`direction` describe when the question "fires": `ge` at
    `value >= threshold`, `le` at `value <= threshold`. `confidence_min` only gates
    Score answers (§6.1); it is `None` for Noul and check questions.
    """

    id: str
    primitive: Noul | Score | Choice | None
    scope: Scope
    severity: Severity
    threshold: float
    direction: Direction
    fix: str
    issue: str
    why: str
    rubric: str
    confidence_min: float | None = None


def _noul(instructions: JSONContent, *, criteria: NoulCriteria | None = None) -> Noul:
    return Noul(instructions=instructions, criteria=criteria)


def _score(instructions: JSONContent, levels: list[str]) -> Score:
    return Score(criteria=levels, instructions=instructions)


# ---------------------------------------------------------------------------
# §5.1 Failure direction (per hunk) -- the ones that matter most.
# ---------------------------------------------------------------------------

_FAILURE_DIRECTION: tuple[Question, ...] = (
    Question(
        id='swallows_exception',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` add an `except` that continues without re-raise, '
                'specific handling, or a stated reason?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Re-raise, handle the specific exception, or add a comment explaining why '
        'continuing is safe in `{symbol}` ({path}).',
        issue='An `except` block continues without re-raising, handling the specific error, or '
        'stating why continuing is safe.',
        why='Swallowing an exception hides a failure the caller never learns about.',
        rubric='5.1',
    ),
    Question(
        id='bare_except',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` add a bare `except:` or `except Exception:` with no re-raise?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Catch the specific exception type, or re-raise after handling it, in `{symbol}` ({path}).',
        issue='A bare `except:` or `except Exception:` catches everything with no re-raise.',
        why='A catch-all hides bugs unrelated to the condition being handled.',
        rubric='5.1',
    ),
    Question(
        id='success_on_unverified',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` add a path that returns success, `True`, or exit 0 '
                'on a condition it did not check?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk',
        severity='blocker',
        threshold=0.6,
        direction='ge',
        fix='Verify the condition before reporting success in `{symbol}` ({path}), or report failure.',
        issue='A path reports success without having checked the condition it claims to satisfy.',
        why='A false positive here looks identical to correct behaviour until it ends a project.',
        rubric='5.1',
    ),
    Question(
        id='indistinguishable_default',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` add a default return value a caller cannot tell apart '
                'from a real value (an empty string for a missing secret, `None` for both '
                '"not found" and "error")?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk',
        severity='blocker',
        threshold=0.6,
        direction='ge',
        fix='Return a distinct sentinel, raise, or use a result type so a caller in `{symbol}` '
        '({path}) can tell the default apart from a real value.',
        issue='A default value is indistinguishable from a real result.',
        why='A caller that cannot tell "missing" from "found" will treat one as the other.',
        rubric='5.1',
    ),
    Question(
        id='auth_passes_on_error',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` add an auth, credential, or permission check that '
                'passes when an exception is raised or a lookup fails?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk',
        severity='blocker',
        threshold=0.5,
        direction='ge',
        fix='Fail closed: deny access on the exception or failed lookup in `{symbol}` ({path}).',
        issue='An auth or permission check passes on an error instead of denying access.',
        why='Failing open on an auth check turns any transient error into a bypass.',
        rubric='5.1',
    ),
    Question(
        id='credential_in_output',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` put a token, key, or password into a log line, '
                'exception message, or `repr`?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk',
        severity='blocker',
        threshold=0.5,
        direction='ge',
        fix='Redact the credential before it reaches a log line, exception message, or `repr` in `{symbol}` ({path}).',
        issue='A credential reaches a log line, exception message, or `repr`.',
        why='A leaked credential in logs or errors is a security incident, not a bug report.',
        rubric='5.1',
    ),
)

# ---------------------------------------------------------------------------
# §5.2 Correctness and fit (per hunk).
# ---------------------------------------------------------------------------

_CORRECTNESS_AND_FIT: tuple[Question, ...] = (
    Question(
        id='duplicates_helper',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` reimplement something already in `neighbours.same_module_helpers`?',
                'inspect': 'hunk.diff',
                'focus': 'Compare against `neighbours.same_module_helpers`.',
            }
        ),
        scope='hunk',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Call the existing helper in `neighbours.same_module_helpers` from `{symbol}` ({path}) '
        'instead of reimplementing it.',
        issue='The new code reimplements an existing helper in the same module.',
        why='A duplicated path drifts from the original and produces inconsistent results.',
        rubric='5.2',
    ),
    Question(
        id='violates_convention',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` do something `conventions` says Never, or skip something it says Always?',
                'inspect': 'hunk.diff',
                'focus': 'Check `conventions` for an explicit Always/Never rule this change breaks.',
            }
        ),
        scope='hunk',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Follow the rule `conventions` states for `{symbol}` ({path}).',
        issue='The change breaks a stated Always/Never rule in the project conventions.',
        why='Convention drift compounds: the next reader copies the exception, not the rule.',
        rubric='5.2',
    ),
    Question(
        id='magic_constant',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` add an unexplained literal that gates behaviour?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Name the literal as a constant with a comment explaining its value in `{symbol}` ({path}).',
        issue='An unexplained literal gates behaviour.',
        why='A magic number carries no reason a future editor can check before changing it.',
        rubric='5.2',
    ),
    Question(
        id='vague_error_message',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` raise or log an error a reader could not act on?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Name what failed and what to do about it in the message raised or logged in `{symbol}` ({path}).',
        issue='An error message gives no actionable detail.',
        why='A vague error sends the next debugger back to the source instead of the fix.',
        rubric='5.2',
    ),
    Question(
        id='mixed_responsibility',
        primitive=_score(
            {
                'question': 'How many distinct concerns (IO, parsing, logic, formatting) does the '
                'new code in `hunk.after` mix in one unit?',
                'inspect': 'hunk.after',
            },
            [
                'The new unit touches one concern only, such as parsing or IO but not both.',
                'The new unit combines two related concerns, such as parsing and validation, in one function or class.',
                (
                    'The new unit combines three or more concerns -- for example IO, parsing, logic, '
                    'and formatting -- in a single function or class.'
                ),
            ],
        ),
        scope='hunk',
        severity='important',
        threshold=1.5,
        direction='ge',
        confidence_min=0.6,
        fix='Split the concerns in `{symbol}` ({path}) into separate units, one per responsibility.',
        issue='The new unit mixes two or more distinct concerns.',
        why='A unit that mixes concerns cannot be tested or changed one concern at a time.',
        rubric='5.2',
    ),
)

# ---------------------------------------------------------------------------
# §5.3 Python rubric (per hunk, python, non-test).
#
# Each smell from `reviewer.md`'s Python rubric becomes a Noul whose
# `criteria.false` carries the "do NOT flag" exception, so the boundary lives in the
# question sent to the model, not in a post-filter written in this codebase.
# ---------------------------------------------------------------------------

_ABSENT_FALSE = 'The shape described in `true` is absent from `hunk.after`.'


def _python_rubric_noul(question: str, true: str, false: str) -> Noul:
    return _noul(
        {'question': question, 'inspect': 'hunk.after'},
        criteria=NoulCriteria(true=true, false=false),
    )


_PYTHON_RUBRIC: tuple[Question, ...] = (
    Question(
        id='class_is_a_function',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add a class with `__init__` and exactly one other method, with no '
            'shared mutable state, no invariants enforced on construction, no resource lifecycle, '
            'and no identity semantics -- a function in a costume?',
            'The diff adds a class whose only method besides `__init__` is one other method, with '
            'no shared mutable state, invariants on construction, resource lifecycle, or identity '
            'semantics.',
            'The class has shared mutable state across calls, invariants enforced on construction, '
            'resource lifecycle (`__enter__`/`__exit__`), or identity semantics.',
        ),
        scope='hunk_python',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Replace the class in `{symbol}` ({path}) with a plain function.',
        issue='A class with `__init__` and one other method is a function in a costume.',
        why='A class with no state or lifecycle to manage adds indirection a function would not.',
        rubric='5.3',
    ),
    Question(
        id='init_only_assigns',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add a hand-rolled `__init__` that only assigns fields, where the '
            'class is not already a `@dataclass` or `NamedTuple`?',
            'The diff adds a hand-rolled `__init__` that only assigns its arguments to `self` '
            'fields, with no other logic.',
            'The class is already a `@dataclass` or `NamedTuple`.',
        ),
        scope='hunk_python',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Replace the hand-rolled `__init__` in `{symbol}` ({path}) with `@dataclass` '
        '(`frozen=True` if immutable) or `NamedTuple`.',
        issue='A hand-rolled `__init__` only assigns fields.',
        why='A plain struct type gives equality, repr, and immutability for free.',
        rubric='5.3',
    ),
    Question(
        id='all_static_class',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add a class whose methods are all `@staticmethod` or never touch `self`?',
            'The diff adds a class whose methods are all `@staticmethod`, or which never reference `self`.',
            _ABSENT_FALSE,
        ),
        scope='hunk_python',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Move the functions in `{symbol}` ({path}) to module level; a module already namespaces them.',
        issue='A class holds only static methods that never touch `self`.',
        why='The class adds a namespace the module already provides.',
        rubric='5.3',
    ),
    Question(
        id='speculative_polymorphism',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add an ABC or `Protocol` with one implementation and no near-term second?',
            'The diff adds an ABC or `Protocol` with exactly one implementation and no second one in sight.',
            'Two or more real implementations of the interface exist in the diff or the module.',
        ),
        scope='hunk_python',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Remove the abstraction in `{symbol}` ({path}) until a second implementation exists.',
        issue='An ABC or `Protocol` has one implementation and no near-term second.',
        why='An abstraction with one implementation is a guess about a future that may not arrive.',
        rubric='5.3',
    ),
    Question(
        id='self_as_config_bag',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add a class where each method uses a different slice of `self`, '
            'rather than shared state?',
            "The diff adds a class where each method reads a different subset of self's fields "
            'rather than sharing state across methods.',
            _ABSENT_FALSE,
        ),
        scope='hunk_python',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Split `{symbol}` ({path}) into functions that take only the fields each one needs.',
        issue='`self` is used as a config bag: each method reads a different slice of it.',
        why='A config bag hides which fields a given method actually depends on.',
        rubric='5.3',
    ),
    Question(
        id='too_many_positional_params',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add a function or method with five or more positional parameters, especially booleans?',
            'The diff adds a function or method with five or more positional parameters, including boolean ones.',
            _ABSENT_FALSE,
        ),
        scope='hunk_python',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Group the parameters of `{symbol}` ({path}) into a config dataclass, or split the function.',
        issue='A function takes five or more positional parameters.',
        why='A long positional signature is easy to call with arguments in the wrong order.',
        rubric='5.3',
    ),
    Question(
        id='boolean_flag_param',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add a boolean parameter that gates which behaviour a function performs?',
            'The diff adds a boolean parameter whose value selects between two different behaviours '
            'inside the function.',
            'The flag mirrors an existing convention already used in `conventions`.',
        ),
        scope='hunk_python',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Split `{symbol}` ({path}) into two functions, one per behaviour, instead of a boolean flag.',
        issue='A boolean parameter gates which behaviour the function performs.',
        why='A flag parameter means the function has two behaviours wearing one name.',
        rubric='5.3',
    ),
    Question(
        id='module_level_mutable_state',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add module-level mutable state?',
            'The diff adds a mutable variable at module level that call sites read or write across calls.',
            'The state is a cache with clear invalidation, or a registry built at import.',
        ),
        scope='hunk_python',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Move the mutable state in `{path}` into an explicit object passed to `{symbol}`.',
        issue='Module-level mutable state is added with no invalidation or import-time justification.',
        why='Hidden global state makes call order and test isolation fragile.',
        rubric='5.3',
    ),
    Question(
        id='temporal_coupling_unenforced',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add top-level functions that must be called in a specific order, '
            'with nothing enforcing that order?',
            'The diff adds top-level functions that must run in a fixed order, with no mechanism enforcing that order.',
            _ABSENT_FALSE,
        ),
        scope='hunk_python',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Enforce the call order in `{symbol}` ({path}) with a lifecycle object or context manager.',
        issue='Functions must be called in a specific order, but nothing enforces it.',
        why='An unenforced call order is a bug waiting for the next caller who skips a step.',
        rubric='5.3',
    ),
    Question(
        id='kwargs_passthrough_undocumented',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add `*args, **kwargs` pass-through with no documentation of what it accepts?',
            'The diff adds `*args, **kwargs` that pass through to another call, with no docstring or '
            'comment naming what is accepted.',
            _ABSENT_FALSE,
        ),
        scope='hunk_python',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Document what `{symbol}` ({path}) forwards through `*args, **kwargs`, or name the parameters.',
        issue='`*args, **kwargs` pass-through is undocumented.',
        why='An undocumented pass-through hides what a caller may legally pass.',
        rubric='5.3',
    ),
    Question(
        id='missing_type_hints_public',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add a new public function or attribute with no type hints?',
            'The diff adds a new public function, method, or attribute with no type hints on its '
            'parameters or return value.',
            'The code is untouched pre-existing code, not new in this diff.',
        ),
        scope='hunk_python',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Add type hints to the public signature of `{symbol}` ({path}).',
        issue='A new public function or attribute has no type hints.',
        why='Missing hints on a public interface push type-checking work onto every caller.',
        rubric='5.3',
    ),
    Question(
        id='missing_type_hints_private',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add a new private function with no type hints?',
            'The diff adds a new private (leading-underscore) function with no type hints.',
            'The code is untouched pre-existing code, not new in this diff.',
        ),
        scope='hunk_python',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Add type hints to `{symbol}` ({path}).',
        issue='A new private function has no type hints.',
        why='Missing hints on new code lose ty coverage from the first commit.',
        rubric='5.3',
    ),
    Question(
        id='abc_over_protocol',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add an ABC for a duck-typed interface where `Protocol` would do?',
            'The diff adds an `abc.ABC` subclass for an interface that only duck-typed callers implement.',
            'The local convention already standardises on ABC for this kind of interface.',
        ),
        scope='hunk_python',
        severity='nit',
        threshold=0.8,
        direction='ge',
        fix='Use `Protocol` instead of an ABC for the interface in `{symbol}` ({path}).',
        issue='An ABC is used where `Protocol` fits a duck-typed interface.',
        why='`Protocol` avoids forcing implementers into an inheritance hierarchy.',
        rubric='5.3',
    ),
    Question(
        id='os_path_over_pathlib',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add `os.path` string manipulation where `pathlib.Path` would do?',
            'The diff adds `os.path` joins or string manipulation for filesystem paths.',
            'The local convention already uses `os.path` throughout this module.',
        ),
        scope='hunk_python',
        severity='nit',
        threshold=0.8,
        direction='ge',
        fix='Use `pathlib.Path` instead of `os.path` string work in `{symbol}` ({path}).',
        issue='`os.path` string manipulation is used where `pathlib.Path` would do.',
        why='`pathlib` composes and type-checks paths; string joins do not.',
        rubric='5.3',
    ),
    Question(
        id='format_over_fstring',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add `%`-formatting or `.format()` where an f-string would do?',
            'The diff adds `%`-formatting or `str.format()` calls to build a string from variables.',
            'The local convention already uses `%` or `.format()` throughout this module.',
        ),
        scope='hunk_python',
        severity='nit',
        threshold=0.8,
        direction='ge',
        fix='Use an f-string instead of `%`-formatting or `.format()` in `{symbol}` ({path}).',
        issue='`%`-formatting or `.format()` is used where an f-string would do.',
        why='An f-string keeps the value next to its placeholder, which is easier to check by eye.',
        rubric='5.3',
    ),
    Question(
        id='constants_over_enum',
        primitive=_python_rubric_noul(
            'Does `hunk.after` add module-level constants used as a closed set where `enum.Enum` '
            'or `StrEnum` would do?',
            'The diff adds two or more module-level constants that together form a closed set of named values.',
            'The local convention already uses module-level constants for closed sets.',
        ),
        scope='hunk_python',
        severity='nit',
        threshold=0.8,
        direction='ge',
        fix='Use `enum.Enum` or `StrEnum` instead of module-level constants in `{symbol}` ({path}).',
        issue='Module-level constants stand in for a closed set that `enum.Enum` would model.',
        why='An enum tells a reader and a type-checker the set is closed; constants do not.',
        rubric='5.3',
    ),
)

# ---------------------------------------------------------------------------
# §5.4 Task satisfaction (change-wide). Per-criterion ids are built by
# `criterion_questions`, not listed here.
# ---------------------------------------------------------------------------

_TASK_SATISFACTION: tuple[Question, ...] = (
    Question(
        id='tests_fitted_to_code',
        primitive=_noul(
            {
                'question': 'Does `test_diff` weaken, remove, or special-case assertions that '
                'existed at the red commit?',
                'inspect': 'test_diff',
            }
        ),
        scope='change',
        severity='blocker',
        threshold=0.6,
        direction='ge',
        fix='Restore the original assertions in the acceptance tests instead of loosening them.',
        issue='An assertion present at the red commit was weakened, removed, or special-cased.',
        why='A test fitted to the code instead of the task proves nothing about the task.',
        rubric='5.4',
    ),
    Question(
        id='scope_creep',
        primitive=_score(
            {
                'question': 'How focused is `diff_summary` on the single change described in `task`?',
                'inspect': 'diff_summary',
            },
            [
                'The diff makes one change with no unrelated edits.',
                'The diff makes one primary change plus a small unrelated tweak alongside it.',
                'The diff bundles two or more independent changes that could each be their own task.',
            ],
        ),
        scope='change',
        severity='important',
        threshold=1.5,
        direction='ge',
        confidence_min=0.6,
        fix='Split the unrelated changes in the diff into their own task.',
        issue='The diff bundles independent changes beyond the task at hand.',
        why='A bundled change is harder to review and to revert independently.',
        rubric='5.4',
    ),
)

# ---------------------------------------------------------------------------
# §5.5 Test adequacy (change-wide and per test hunk).
# ---------------------------------------------------------------------------

_TEST_ADEQUACY: tuple[Question, ...] = (
    Question(
        id='new_behaviour_untested',
        primitive=_score(
            {
                'question': 'How much of the new behaviour in `src_diff` is exercised by '
                '`acceptance_tests` and `test_diff`?',
                'inspect': 'src_diff',
            },
            [
                (
                    'None of the new branches or paths added in `src_diff` are exercised by any test '
                    'in `acceptance_tests` or `test_diff`.'
                ),
                'Some of the new branches are exercised, but at least one added path has no covering test.',
                'Most of the new branches are exercised, with only a minor edge case left untested.',
                'All new branches and paths added in `src_diff` are exercised by a test.',
            ],
        ),
        scope='change',
        severity='important',
        threshold=1.0,
        direction='le',
        confidence_min=0.6,
        fix='Add a test exercising the untested new behaviour in `src_diff`.',
        issue='New behaviour is added with no test exercising it.',
        why='Untested behaviour has no proof it does what the task asked.',
        rubric='5.5',
    ),
    Question(
        id='touches_high_risk',
        primitive=_noul(
            {
                'question': 'Does `src_diff` touch credentials, auth, or disk writes outside the repo?',
                'inspect': 'src_diff',
            }
        ),
        scope='change',
        severity='modifier',
        threshold=0.6,
        direction='ge',
        fix='N/A: this question only raises the severity of `new_behaviour_untested`, in `compose.py`.',
        issue='The change touches high-risk behaviour: credentials, auth, or disk writes outside the repo.',
        why='Untested high-risk behaviour is a blocker, not merely an important finding.',
        rubric='5.5',
    ),
    Question(
        id='patches_unit_under_test',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` patch the unit under test rather than its collaborators?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk_test',
        severity='important',
        threshold=0.7,
        direction='ge',
        fix='Patch the collaborator instead of the unit under test in `{symbol}` ({path}).',
        issue='The test patches the unit under test rather than its collaborators.',
        why='A test that patches its own subject tests the mock, not the code.',
        rubric='5.5',
    ),
    Question(
        id='mock_hides_integration',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` add a heavy `MagicMock` where a small fake would show '
                'a broken integration?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk_test',
        severity='minor',
        threshold=0.75,
        direction='ge',
        fix='Replace the `MagicMock` in `{symbol}` ({path}) with a small fake that would fail on a broken integration.',
        issue='A heavy `MagicMock` would pass even if the real integration were broken.',
        why='A mock that accepts anything hides the exact failure a test exists to catch.',
        rubric='5.5',
    ),
    Question(
        id='unittest_testcase_style',
        primitive=_noul(
            {
                'question': 'Does `hunk.diff` add a new `unittest.TestCase` instead of a plain '
                'pytest function or fixture?',
                'inspect': 'hunk.diff',
            }
        ),
        scope='hunk_test',
        severity='nit',
        threshold=0.8,
        direction='ge',
        fix='Use a plain pytest function and fixtures instead of `unittest.TestCase` in `{path}`.',
        issue='A new test uses `unittest.TestCase` instead of plain pytest.',
        why='Mixing test styles costs a reader the fixture and parametrize conventions pytest gives for free.',
        rubric='5.5',
    ),
)

# ---------------------------------------------------------------------------
# §4.1 Deterministic-check finding ids. `primitive=None`: these never go to the
# model, but severity and fix text live here so there is one home for both.
# ---------------------------------------------------------------------------

_DETERMINISTIC_CHECKS: tuple[Question, ...] = (
    Question(
        id='red_proof_missing',
        primitive=None,
        scope='check',
        severity='blocker',
        threshold=1.0,
        direction='ge',
        fix='Commit the acceptance tests alone, watch them fail for the right reason, then implement.',
        issue='No red-sha was given, or the acceptance tests did not fail at the red commit.',
        why='Without a failing-first proof, nothing shows the tests exercise the task, not the code.',
        rubric=_CHECK_RUBRIC,
    ),
    Question(
        id='acceptance_tests_edited',
        primitive=None,
        scope='check',
        severity='blocker',
        threshold=1.0,
        direction='ge',
        fix='Revert the edit to `{path}`, or report the criterion as wrong instead of changing its test.',
        issue='An acceptance test file changed between the red commit and HEAD.',
        why='A test fitted to the code after the fact proves nothing about the task.',
        rubric=_CHECK_RUBRIC,
    ),
    Question(
        id='gate_failed',
        primitive=None,
        scope='check',
        severity='blocker',
        threshold=1.0,
        direction='ge',
        fix='Fix the first failing stage of `make check` and re-run the whole gate.',
        issue='`make check` did not exit 0.',
        why='A red gate means the change is not done, regardless of what the diff looks like.',
        rubric=_CHECK_RUBRIC,
    ),
)

CATALOG: tuple[Question, ...] = (
    _FAILURE_DIRECTION
    + _CORRECTNESS_AND_FIT
    + _PYTHON_RUBRIC
    + _TASK_SATISFACTION
    + _TEST_ADEQUACY
    + _DETERMINISTIC_CHECKS
)


def criterion_questions(task: Task) -> dict[str, Question]:
    """Build `criterion_{n}_satisfied` / `criterion_{n}_tested` for `task`, §5.4.

    Not part of `CATALOG`: these ids depend on the task file, one pair per
    acceptance criterion, embedding the criterion's own text in the instructions.
    The question id itself is never sent to the model.
    """
    questions: dict[str, Question] = {}
    for n, criterion in enumerate(task.acceptance, start=1):
        satisfied_id = f'criterion_{n}_satisfied'
        questions[satisfied_id] = Question(
            id=satisfied_id,
            primitive=_noul(
                {
                    'question': f'Does `src_diff` implement this acceptance criterion: {criterion}',
                    'inspect': 'src_diff',
                }
            ),
            scope='change',
            severity='blocker',
            threshold=0.4,
            direction='le',
            fix='Implement the acceptance criterion in the diff, or report it as wrong rather than editing its test.',
            issue=f'The diff does not appear to implement: {criterion}',
            why='An unimplemented criterion means the task is not done, whatever else the diff contains.',
            rubric='5.4',
        )

        tested_id = f'criterion_{n}_tested'
        questions[tested_id] = Question(
            id=tested_id,
            primitive=_noul(
                {
                    'question': f'Does `acceptance_tests` exercise this acceptance criterion: {criterion}',
                    'inspect': 'acceptance_tests',
                }
            ),
            scope='change',
            severity='important',
            threshold=0.4,
            direction='le',
            fix='Add an acceptance test exercising this criterion.',
            issue=f'No acceptance test appears to exercise: {criterion}',
            why='A criterion with no test has no proof it is met, today or after the next change.',
            rubric='5.4',
        )
    return questions


def questions_for(scope: Scope, language: str | None = None, is_test: bool = False) -> dict[str, Question]:
    """Select the catalog subset for one `scope`.

    For `scope='hunk'`, `language` and `is_test` decide which extra bucket joins the
    base `hunk` questions: Python non-test hunks get `hunk_python`, any test hunk
    gets `hunk_test`, everything else gets `hunk` alone. For every other scope
    (`change`, `check`) `language`/`is_test` are ignored.
    """
    if scope != 'hunk':
        return {q.id: q for q in CATALOG if q.scope == scope}

    scopes: set[Scope] = {'hunk'}
    if is_test:
        scopes.add('hunk_test')
    elif language == 'python':
        scopes.add('hunk_python')

    return {q.id: q for q in CATALOG if q.scope in scopes}
