"""Tests for the integrity checker.

Each check gets a fixture that violates it and, where the distinction matters, one
that must *not* trip it. A check that never fires is indistinguishable from a check
that always agrees with the build.
"""

from __future__ import annotations

import check_governance as cg

from .ledger import ControlSpec, Ledger

# Never write a literal `governance: enforces DEC-N` in this file. Check 4 scans the
# whole tree, including tests, so any literal here is a real pragma as far as the
# checker is concerned -- and becomes a dangling one the moment that decision is
# superseded. Every pragma below is assembled from this token instead.
PRAGMA_TOKEN = 'governance: enforces '
PRAGMA = f'# {PRAGMA_TOKEN}{{id}}\n'
POISON = 'NEVER-SHOW-THIS-TO-AN-AGENT'


def codes(failures: list[cg.Failure]) -> list[int]:
    return sorted({failure.check for failure in failures})


# --- Check 1: a live rule has teeth ----------------------------------------------


def test_check_1_fires_on_a_live_decision_with_no_controls(ledger: Ledger) -> None:
    ledger.decision('DEC-1')

    failures = cg.check_1_decisions_have_controls(ledger.load())

    assert codes(failures) == [1]
    assert failures[0].decision == 'DEC-1'


def test_check_1_exempts_a_superseded_decision(ledger: Ledger) -> None:
    """History is a record, not a constraint."""
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-2')
    ledger.decision('DEC-2', controls=[ControlSpec(path='a.py')])

    assert cg.check_1_decisions_have_controls(ledger.load()) == []


def test_check_1_exempts_a_draft(ledger: Ledger) -> None:
    ledger.decision('DEC-1', status='draft')

    assert cg.check_1_decisions_have_controls(ledger.load()) == []


# --- Check 2: listed controls exist ----------------------------------------------


def test_check_2_fires_when_a_listed_control_is_missing(ledger: Ledger) -> None:
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/gone.py')])

    failures = cg.check_2_control_paths_exist(ledger.load())

    assert codes(failures) == [2]
    assert failures[0].file == 'controls/fitness/gone.py'


def test_check_2_passes_when_the_control_exists(ledger: Ledger) -> None:
    ledger.control('controls/fitness/here.py', PRAGMA.format(id='DEC-1'))
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/here.py')])

    assert cg.check_2_control_paths_exist(ledger.load()) == []


# --- Check 3: the control-to-decision tether -------------------------------------


def test_check_3_catches_a_control_that_lost_its_pragma(ledger: Ledger) -> None:
    """Experiment (b). Stripping the pragma is often the first sign of evasion."""
    ledger.control('controls/fitness/a.py', 'print("logic, but no pragma")\n')
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])

    failures = cg.check_3_pragmas_present(ledger.load())

    assert codes(failures) == [3]
    assert failures[0].decision == 'DEC-1'
    assert failures[0].file == 'controls/fitness/a.py'
    assert PRAGMA_TOKEN + 'DEC-1' in failures[0].remedy


def test_check_3_passes_when_the_pragma_is_present(ledger: Ledger) -> None:
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-1'))
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])

    assert cg.check_3_pragmas_present(ledger.load()) == []


def test_check_3_accepts_a_control_enforcing_several_decisions(ledger: Ledger) -> None:
    ledger.control('controls/fitness/a.py', f'# {PRAGMA_TOKEN}DEC-1, DEC-2\n')
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])
    ledger.decision('DEC-2', controls=[ControlSpec(path='controls/fitness/a.py')])

    assert cg.check_3_pragmas_present(ledger.load()) == []


def test_check_3_does_not_require_a_pragma_for_a_superseded_decision(ledger: Ledger) -> None:
    """Without this exemption a correct supersession would be impossible.

    Retargeting the control's pragma from DEC-1 to DEC-2 necessarily leaves DEC-1
    without one. That is the desired end state, not a violation.
    """
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-2'))
    ledger.decision(
        'DEC-1', status='superseded', superseded_by='DEC-2', controls=[ControlSpec(path='controls/fitness/a.py')]
    )
    ledger.decision('DEC-2', controls=[ControlSpec(path='controls/fitness/a.py')])

    assert cg.check_3_pragmas_present(ledger.load()) == []


def test_check_3_ignores_external_controls(ledger: Ledger) -> None:
    ledger.control('config.toml', 'a = 1\n')
    ledger.decision('DEC-1', controls=[ControlSpec(path='config.toml', pragma='external', sha256='abc')])

    assert cg.check_3_pragmas_present(ledger.load()) == []


# --- Check 4: every pragma in the tree resolves ----------------------------------


def test_check_4_catches_a_pragma_naming_a_decision_that_does_not_exist(ledger: Ledger) -> None:
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-404'))
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])

    failures = cg.check_4_pragmas_resolve(ledger.load())

    assert codes(failures) == [4]
    assert failures[0].decision == 'DEC-404'


def test_check_4_catches_a_pragma_naming_a_superseded_decision(ledger: Ledger) -> None:
    """A pragma left pointing at the old rule is an unfinished supersession."""
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-1'))
    ledger.decision(
        'DEC-1', status='superseded', superseded_by='DEC-2', controls=[ControlSpec(path='controls/fitness/a.py')]
    )
    ledger.decision('DEC-2', controls=[ControlSpec(path='controls/fitness/a.py')])

    failures = cg.check_4_pragmas_resolve(ledger.load())

    assert codes(failures) == [4]
    assert 'superseded' in failures[0].problem


def test_check_4_catches_a_pragma_naming_a_draft(ledger: Ledger) -> None:
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-1'))
    ledger.decision('DEC-1', status='draft', controls=[ControlSpec(path='controls/fitness/a.py')])

    assert codes(cg.check_4_pragmas_resolve(ledger.load())) == [4]


def test_check_4_skips_markdown(ledger: Ledger) -> None:
    """Prose legitimately quotes example pragmas; AGENTS.md and the spec both do."""
    prose = f'Add a line reading `{PRAGMA_TOKEN}DEC-404`.\n'
    (ledger.root / 'AGENTS.md').write_text(prose, encoding='utf-8')
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-1'))
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])

    assert cg.check_4_pragmas_resolve(ledger.load()) == []


def test_check_4_finds_a_pragma_outside_the_controls_directory(ledger: Ledger) -> None:
    """ "Anywhere in the tree" is the point: a stray pragma in src/ still has to resolve."""
    ledger.control('src/app/sneaky.py', PRAGMA.format(id='DEC-404'))
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-1'))
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])

    failures = cg.check_4_pragmas_resolve(ledger.load())

    assert codes(failures) == [4]
    assert failures[0].file == 'src/app/sneaky.py'


# --- Check 5: content hashes for comment-less controls ---------------------------


def test_check_5_catches_a_hash_mismatch(ledger: Ledger) -> None:
    config = ledger.control('config.toml', 'line-length = 120\n')
    recorded = cg.sha256_of(config)
    ledger.decision('DEC-1', controls=[ControlSpec(path='config.toml', pragma='external', sha256=recorded)])

    config.write_text('line-length = 500\n', encoding='utf-8')
    failures = cg.check_5_external_hashes(ledger.load())

    assert codes(failures) == [5]
    assert recorded in failures[0].problem
    assert 'reading the diff' in failures[0].remedy


def test_check_5_passes_on_a_matching_hash(ledger: Ledger) -> None:
    config = ledger.control('config.toml', 'line-length = 120\n')
    ledger.decision('DEC-1', controls=[ControlSpec(path='config.toml', pragma='external', sha256=cg.sha256_of(config))])

    assert cg.check_5_external_hashes(ledger.load()) == []


def test_check_5_catches_an_external_control_with_no_recorded_hash(ledger: Ledger) -> None:
    config = ledger.control('config.toml', 'line-length = 120\n')
    ledger.decision('DEC-1', controls=[ControlSpec(path='config.toml', pragma='external')])

    failures = cg.check_5_external_hashes(ledger.load())

    assert codes(failures) == [5]
    assert cg.sha256_of(config) in failures[0].remedy


def test_check_5_hash_comparison_is_case_insensitive(ledger: Ledger) -> None:
    config = ledger.control('config.toml', 'line-length = 120\n')
    ledger.decision(
        'DEC-1',
        controls=[ControlSpec(path='config.toml', pragma='external', sha256=cg.sha256_of(config).upper())],
    )

    assert cg.check_5_external_hashes(ledger.load()) == []


# --- Check 7: no dead rule text under views/ -------------------------------------


def test_check_7_catches_a_superseded_rule_leaking_into_the_view(ledger: Ledger) -> None:
    """The most damaging failure in the system. Treat any hit as a generator bug."""
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-2', rule=f'{POISON}.')
    ledger.decision('DEC-2', controls=[ControlSpec(path='a.py')])
    ledger.view(f'- **[DEC-2] {POISON}.** (block)\n')

    failures = cg.check_7_no_dead_rules_in_views(ledger.load())

    assert codes(failures) == [7]
    assert 'rule text appears verbatim' in failures[0].problem


def test_check_7_catches_a_dead_decision_tag_in_the_view(ledger: Ledger) -> None:
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-2', rule='Old wording.')
    ledger.decision('DEC-2', controls=[ControlSpec(path='a.py')])
    ledger.view('- **[DEC-1] Something entirely different.** (block)\n')

    assert codes(cg.check_7_no_dead_rules_in_views(ledger.load())) == [7]


def test_check_7_passes_on_a_correctly_generated_view(ledger: Ledger) -> None:
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-2', rule=f'{POISON}.')
    ledger.decision('DEC-2', rule='The replacement rule.', controls=[ControlSpec(path='a.py')])
    ledger.generate()

    assert cg.check_7_no_dead_rules_in_views(ledger.load()) == []


# --- Check 8: bookkeeping ---------------------------------------------------------


def test_check_8_catches_a_duplicate_id(ledger: Ledger) -> None:
    ledger.decision('DEC-1')
    (ledger.decisions_dir / 'DEC-1-again.md').write_text(
        '---\nid: DEC-1\ntitle: t\nstatus: accepted\nkind: constraint\n---\n\n## Rule\nx\n', encoding='utf-8'
    )

    assert codes(cg.check_8_ids_and_supersessions(ledger.load())) == [8]


def test_check_8_catches_a_dangling_superseded_by(ledger: Ledger) -> None:
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-99')

    failures = cg.check_8_ids_and_supersessions(ledger.load())

    assert codes(failures) == [8]
    assert 'DEC-99' in failures[0].problem


def test_check_8_catches_superseded_by_set_while_status_is_accepted(ledger: Ledger) -> None:
    """A half-finished supersession leaves two live rules if nobody notices."""
    ledger.decision('DEC-1', status='accepted', superseded_by='DEC-2')
    ledger.decision('DEC-2')

    failures = cg.check_8_ids_and_supersessions(ledger.load())

    assert codes(failures) == [8]
    assert 'status: accepted' in failures[0].problem


def test_check_8_catches_a_supersession_pointing_at_a_dead_decision(ledger: Ledger) -> None:
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-2')
    ledger.decision('DEC-2', status='draft')

    assert codes(cg.check_8_ids_and_supersessions(ledger.load())) == [8]


def test_check_8_passes_on_a_correct_supersession(ledger: Ledger) -> None:
    ledger.decision('DEC-1', status='superseded', superseded_by='DEC-2')
    ledger.decision('DEC-2')

    assert cg.check_8_ids_and_supersessions(ledger.load()) == []


# --- End to end -------------------------------------------------------------------


def test_a_correct_supersession_passes_every_check(ledger: Ledger) -> None:
    """Experiment (c) in miniature: the whole workflow, green, with no trace of the old rule."""
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-2'))
    ledger.decision(
        'DEC-1',
        status='superseded',
        superseded_by='DEC-2',
        rule=f'{POISON}.',
        controls=[ControlSpec(path='controls/fitness/a.py')],
    )
    ledger.decision('DEC-2', rule='The replacement rule.', controls=[ControlSpec(path='controls/fitness/a.py')])
    ledger.generate()

    assert cg.run(skip_suite=True) == []
    assert POISON not in ledger.rules_text()


def test_run_reports_a_stale_view_via_check_6(ledger: Ledger) -> None:
    ledger.control('controls/fitness/a.py', PRAGMA.format(id='DEC-1'))
    ledger.decision('DEC-1', rule='Original.', controls=[ControlSpec(path='controls/fitness/a.py')])
    ledger.generate()
    ledger.view(ledger.rules_text().replace('Original', 'Tampered'))

    assert 6 in codes(cg.run(skip_suite=True))


def test_run_surfaces_a_parse_error_before_anything_else(ledger: Ledger) -> None:
    (ledger.decisions_dir / 'DEC-9-broken.md').write_text('no frontmatter here\n', encoding='utf-8')

    failures = cg.run(skip_suite=True)

    assert len(failures) == 1
    assert failures[0].check == 0


def test_every_failure_names_the_problem_and_a_remedy(ledger: Ledger) -> None:
    """A check that says only FAILED trains people to ignore it."""
    ledger.control('controls/fitness/a.py', 'print("no pragma")\n')
    ledger.decision('DEC-1', controls=[ControlSpec(path='controls/fitness/a.py')])
    ledger.decision('DEC-2')

    for failure in cg.run(skip_suite=True):
        rendered = failure.render()
        assert failure.problem
        assert failure.remedy
        assert rendered.startswith('FAIL [check ')
        assert '->' in rendered
