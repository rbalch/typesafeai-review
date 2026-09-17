"""Tests for the generator.

The generator and the integrity checker are the two things everything else in this
repo trusts, so they get real tests rather than a smoke run.
"""

from __future__ import annotations

import json

import build_views as bv
import pytest

from .ledger import ControlSpec, Ledger

POISON = 'NEVER-SHOW-THIS-TO-AN-AGENT'


# --- The rule that matters most: dead rules never reach the view ----------------


def test_superseded_decision_is_excluded_from_the_view(ledger: Ledger) -> None:
    """The entire design exists to stop superseded text reaching an agent's context."""
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-2', rule=f'{POISON}.')
    ledger.decision('DEC-2', rule='The replacement rule.')

    rules = ledger.build()[bv.RULES_PATH]

    assert POISON not in rules
    assert '[DEC-1]' not in rules
    assert 'The replacement rule.' in rules
    assert '[DEC-2]' in rules


def test_draft_decision_is_excluded_from_the_view(ledger: Ledger) -> None:
    """Drafts are invisible to agents by design."""
    ledger.decision('DEC-1', status='draft', rule=f'{POISON}.')

    rules = ledger.build()[bv.RULES_PATH]

    assert POISON not in rules
    assert '[DEC-1]' not in rules


def test_accepted_decision_carrying_superseded_by_is_excluded(ledger: Ledger) -> None:
    """Half-finished supersession: the status says accepted, the pointer says otherwise.

    The view must take the cautious reading. `check_governance` check 8 reports the
    bookkeeping error separately, but the rule must not reach an agent in the meantime.
    """
    ledger.decision('DEC-1', status='accepted', superseded_by='DEC-2', rule=f'{POISON}.')
    ledger.decision('DEC-2', rule='The replacement rule.')

    rules = ledger.build()[bv.RULES_PATH]

    assert POISON not in rules
    assert '[DEC-1]' not in rules


def test_superseded_decision_still_appears_in_the_registry(ledger: Ledger) -> None:
    """The registry is an index for humans and tooling, not an agent-facing view."""
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-2')
    ledger.decision('DEC-2')

    registry = json.loads(ledger.build()[bv.REGISTRY_PATH])
    entries = {entry['id']: entry for entry in registry['decisions']}

    assert entries['DEC-1']['status'] == 'superseded'
    assert entries['DEC-1']['superseded_by'] == 'DEC-2'
    assert entries['DEC-2']['superseded_by'] is None


# --- Determinism ----------------------------------------------------------------


def test_two_builds_are_byte_identical(ledger: Ledger) -> None:
    """Non-deterministic output makes --check a coin flip and the harness toothless."""
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])
    ledger.decision('DEC-2', controls=[ControlSpec(path='controls/fitness/b.py')])

    assert ledger.build() == ledger.build()


def test_decisions_sort_numerically_not_lexicographically(ledger: Ledger) -> None:
    """A lexicographic sort is deterministic too -- and silently wrong from DEC-10 on."""
    for id_ in ('DEC-10', 'DEC-2', 'DEC-1', 'DEC-20', 'DEC-3'):
        ledger.decision(id_, rule=f'Rule for {id_}.')

    rules = ledger.build()[bv.RULES_PATH]
    order = [line.split(']')[0].split('[')[1] for line in rules.splitlines() if line.startswith('- **[')]

    assert order == ['DEC-1', 'DEC-2', 'DEC-3', 'DEC-10', 'DEC-20']


def test_generated_output_contains_no_timestamp(ledger: Ledger) -> None:
    ledger.decision('DEC-1')
    artifacts = ledger.build()

    for content in artifacts.values():
        assert '2026-07-30' not in content, 'a created: date leaked into generated output'


def test_registry_json_has_sorted_keys_and_a_trailing_newline(ledger: Ledger) -> None:
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])

    registry = ledger.build()[bv.REGISTRY_PATH]

    assert registry.endswith('\n')
    parsed = json.loads(registry)
    assert list(parsed) == sorted(parsed)
    assert list(parsed['decisions'][0]) == sorted(parsed['decisions'][0])


# --- Rendering ------------------------------------------------------------------


def test_rule_text_is_flattened_without_loss(ledger: Ledger) -> None:
    """Paragraphs are joined, never truncated. A half-rendered rule is a lie."""
    ledger.decision(
        'DEC-1',
        rule='First sentence.\n\nSecond paragraph that must survive.\n',
        body='\n## Context\nShould not appear in the view.\n',
    )

    rules = ledger.build()[bv.RULES_PATH]

    assert 'First sentence. Second paragraph that must survive.' in rules
    assert 'Should not appear' not in rules


def test_enforcement_is_derived_from_the_controls(ledger: Ledger) -> None:
    """Block wins over warn, and a decision with no controls is advisory."""
    ledger.decision('DEC-1', rule='Blocking.', controls=[ControlSpec(path='a.py', enforcement='block')])
    ledger.decision('DEC-2', rule='Advisory.', controls=[ControlSpec(path='b.py', enforcement='warn')])
    ledger.decision(
        'DEC-3',
        rule='Mixed.',
        controls=[ControlSpec(path='c.py', enforcement='warn'), ControlSpec(path='d.py', enforcement='block')],
    )
    ledger.decision('DEC-4', rule='Toothless.')

    rules = ledger.build()[bv.RULES_PATH]

    assert '**[DEC-1] Blocking.** (block)' in rules
    assert '**[DEC-2] Advisory.** (warn)' in rules
    assert '**[DEC-3] Mixed.** (block)' in rules
    assert '**[DEC-4] Toothless.** (warn)' in rules


def test_empty_ledger_says_so_rather_than_rendering_nothing(ledger: Ledger) -> None:
    """A blank view is indistinguishable from a generator that never ran."""
    rules = ledger.build()[bv.RULES_PATH]

    assert 'No rules are currently in force' in rules
    assert bv.GENERATED_HEADER in rules


def test_the_view_carries_the_do_not_edit_header(ledger: Ledger) -> None:
    ledger.decision('DEC-1')
    assert ledger.build()[bv.RULES_PATH].startswith(bv.GENERATED_HEADER)


# --- --check --------------------------------------------------------------------


def test_check_passes_on_freshly_generated_files(ledger: Ledger) -> None:
    ledger.decision('DEC-1')
    ledger.generate()

    assert bv.check(ledger.build()) == 0


def test_check_catches_a_hand_edited_view(ledger: Ledger) -> None:
    """Experiment (d): change a word in RULES.md and the gate must notice."""
    ledger.decision('DEC-1', rule='The original wording.')
    ledger.generate()

    tampered = ledger.rules_text().replace('original', 'tampered')
    ledger.view(tampered)

    assert bv.check(ledger.build()) == 1


def test_check_catches_a_view_that_was_never_generated(ledger: Ledger) -> None:
    ledger.decision('DEC-1')

    assert bv.check(ledger.build()) == 1


def test_check_catches_a_stale_view_after_a_decision_changes(ledger: Ledger) -> None:
    """ "Forgot to rebuild" and "hand-edited a generated file" are the same failure here."""
    ledger.decision('DEC-1', rule='The first wording.')
    ledger.generate()
    ledger.decision('DEC-1', rule='A revised wording.')

    assert bv.check(ledger.build()) == 1


def test_check_reports_a_diff_naming_the_file(ledger: Ledger, capsys: pytest.CaptureFixture[str]) -> None:
    """A check that says only FAILED trains people to ignore it."""
    ledger.decision('DEC-1', rule='The original wording.')
    ledger.generate()
    ledger.view(ledger.rules_text().replace('original', 'tampered'))

    bv.check(ledger.build())
    captured = capsys.readouterr().err

    assert 'governance/views/RULES.md' in captured
    assert '-' in captured and '+' in captured
    assert 'make views' in captured


# --- Malformed decisions --------------------------------------------------------


@pytest.mark.parametrize(
    ('name', 'content', 'expected'),
    [
        ('no frontmatter', '# just a heading\n', 'missing YAML frontmatter'),
        (
            'no rule section',
            '---\nid: DEC-3\ntitle: t\nstatus: accepted\nkind: constraint\n---\n\n## Context\nx\n',
            'no `## Rule` section',
        ),
        (
            'empty rule section',
            '---\nid: DEC-3\ntitle: t\nstatus: accepted\nkind: constraint\n---\n\n## Rule\n\n## Context\nx\n',
            'no `## Rule` section',
        ),
        (
            'malformed id',
            '---\nid: NOPE\ntitle: t\nstatus: accepted\nkind: constraint\n---\n\n## Rule\nx\n',
            'must look like DEC-<number>',
        ),
        (
            'missing status',
            '---\nid: DEC-3\ntitle: t\nkind: constraint\n---\n\n## Rule\nx\n',
            'missing `status`',
        ),
        (
            'control missing a field',
            (
                '---\nid: DEC-4\ntitle: t\nstatus: accepted\nkind: constraint\n'
                'controls:\n  - path: a.py\n    type: lint\n---\n\n## Rule\nx\n'
            ),
            'missing `enforcement`',
        ),
        (
            'controls not a list',
            '---\nid: DEC-5\ntitle: t\nstatus: accepted\nkind: constraint\ncontrols: nope\n---\n\n## Rule\nx\n',
            '`controls` must be a list',
        ),
    ],
)
def test_malformed_decision_raises_a_named_error(ledger: Ledger, name: str, content: str, expected: str) -> None:
    """Every parse failure names the file and the problem, never a bare traceback."""
    path = ledger.decisions_dir / 'DEC-99-broken.md'
    path.write_text(content, encoding='utf-8')

    with pytest.raises(bv.DecisionError, match=expected.replace('`', '.').replace('<', '.').replace('>', '.')):
        bv.parse_decision(path)


def test_null_like_frontmatter_values_are_all_treated_as_absent(ledger: Ledger) -> None:
    for id_, literal in (('DEC-1', 'null'), ('DEC-2', '~'), ('DEC-3', "''")):
        path = ledger.decisions_dir / f'{id_}-fixture.md'
        path.write_text(
            f'---\nid: {id_}\ntitle: t\nstatus: accepted\nkind: constraint\n'
            f'superseded_by: {literal}\n---\n\n## Rule\nRule for {id_}.\n',
            encoding='utf-8',
        )

    assert all(decision.superseded_by is None for decision in ledger.load())


def test_relative_falls_back_to_an_absolute_path_outside_the_repo(tmp_path) -> None:
    """Fixtures live outside the tree; a path helper that raises there breaks every test."""
    outsider = tmp_path / 'somewhere.md'
    outsider.write_text('x', encoding='utf-8')

    assert bv.relative(outsider) == outsider.as_posix()
