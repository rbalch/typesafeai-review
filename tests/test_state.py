"""Acceptance tests for T-05: per-hunk and change-wide state shapes.

One test per acceptance clause in tasks/typesafe-reviewer/T-05-state-and-dump.md.
`build_hunk_states` / `build_change_state` are exercised directly against
`slicing.Change` objects built by hand (no git needed for those); `load_conventions`
and `load_acceptance_tests` are exercised against throwaway git repos, since they
read `AGENTS.md` and `git show` respectively.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import typesafe_review.state as state_module
from typesafe_review.slicing import Change, FileSummary, Hunk, Neighbours
from typesafe_review.state import (
    MAX_HUNK_STATE_BYTES,
    MAX_REQUEST_TOKENS,
    StateError,
    build_change_state,
    build_hunk_states,
    load_acceptance_tests,
    load_conventions,
)
from typesafe_review.taskfile import Task


def make_hunk(path: str = 'src/pkg/config.py', diff_size: int = 0) -> Hunk:
    diff = '@@ -1,1 +1,1 @@ def f\n-old\n+new\n'
    if diff_size:
        diff = diff + ('x' * diff_size)
    return Hunk(
        path=path,
        header='@@ -40,12 +40,20 @@ def resolve_bind_address',
        diff=diff,
        after='def resolve_bind_address():\n    ...\n',
        symbol='resolve_bind_address',
        language='python',
        is_test=False,
        is_new=False,
        is_deleted_only=False,
        truncated=False,
        neighbours=Neighbours(
            same_module_helpers=['def helper(x: int) -> int'],
            tests_touching_file=['tests/test_config.py::test_bind_address_default'],
        ),
    )


def make_change(hunks: list[Hunk] | None = None) -> Change:
    if hunks is None:
        hunks = [make_hunk()]
    summary = [FileSummary(path='src/pkg/config.py', added=30, removed=4, is_test=False)]
    return Change(hunks=hunks, summary=summary, src_diff='src diff text', test_diff='test diff text')


def make_task() -> Task:
    return Task(
        id='T-02', title='Do the thing', acceptance=['first bullet', 'second bullet'], criteria_text='full text'
    )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 't@example.com')
    _git(repo, 'config', 'user.name', 'Test')
    return repo


def commit(repo: Path, message: str) -> str:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', message)
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, check=True, capture_output=True)
    return result.stdout.decode().strip()


def test_max_request_tokens_is_the_calibrated_16000_budget():
    # 2026-09-21 probe (see the constant's comment): the API caps a request at
    # ~32K real input tokens; bytes/4 undercounts by ~1.15x on real code and ~2x
    # on number-heavy text, so 16,000 x 2 sits at the ceiling and real code well
    # under it, while PR #15's 13,410-token change request clears the budget.
    assert MAX_REQUEST_TOKENS == 16000


def test_hunk_state_keys_match_spec_shape_exactly():
    states = build_hunk_states(make_task(), make_change(), 'some conventions text')
    assert len(states) == 1
    state = states[0]
    assert set(state.keys()) == {'task', 'file', 'hunk', 'neighbours', 'conventions'}
    assert set(state['file'].keys()) == {'path', 'language', 'is_test', 'is_new'}
    assert set(state['hunk'].keys()) == {'header', 'diff', 'after'}
    assert set(state['neighbours'].keys()) == {'same_module_helpers', 'tests_touching_file'}


def test_missing_agents_md_gives_empty_conventions(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / 'README.md').write_text('no AGENTS.md here\n')
    commit(repo, 'no AGENTS.md here')

    assert load_conventions(repo) == ''


def test_present_agents_md_joins_the_three_sections_and_skips_missing_ones(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / 'AGENTS.md').write_text(
        '# Title\n\n## Always\nAlways do X.\n\n## Never\nNever do Y.\n\n## Some Other Section\nirrelevant\n'
    )
    commit(repo, 'add AGENTS.md')

    conventions = load_conventions(repo)

    assert 'Always do X.' in conventions
    assert 'Never do Y.' in conventions
    assert 'irrelevant' not in conventions
    # Architectural shape is absent from this AGENTS.md; only the present two
    # sections contribute, joined by a blank line, not an error.
    assert conventions == 'Always do X.\n\nNever do Y.'


def test_task_none_serialises_as_null():
    states = build_hunk_states(None, make_change(), '')
    encoded = json.dumps(states[0])
    assert '"task": null' in encoded
    assert states[0]['task'] is None

    change_state = build_change_state(None, make_change(), '')
    change_encoded = json.dumps(change_state)
    assert '"task": null' in change_encoded
    assert change_state['task'] is None


def test_change_state_diff_summary_counts_match_diff_and_rows_have_four_keys():
    change_state = build_change_state(make_task(), make_change(), '')
    assert len(change_state['diff_summary']) == 1
    row = change_state['diff_summary'][0]
    assert row == {'path': 'src/pkg/config.py', 'added': 30, 'removed': 4, 'is_test': False}
    assert set(row.keys()) == {'path', 'added', 'removed', 'is_test'}


def test_hunk_state_over_1mb_raises_error_naming_the_hunk():
    huge_hunk = make_hunk(path='src/pkg/huge.py', diff_size=MAX_HUNK_STATE_BYTES + 1)

    with pytest.raises(StateError) as exc_info:
        build_hunk_states(None, make_change([huge_hunk]), '')

    assert 'src/pkg/huge.py' in str(exc_info.value)


def test_load_acceptance_tests_with_no_red_sha_is_empty(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / 'README.md').write_text('only commit\n')
    commit(repo, 'only commit')

    assert load_acceptance_tests(repo, None) == ''


def test_load_acceptance_tests_reads_test_files_at_red_sha(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_thing.py').write_text('def test_thing():\n    assert True\n')
    red_sha = commit(repo, 'red: acceptance test')
    (repo / 'tests' / 'test_thing.py').write_text('def test_thing():\n    assert True  # unchanged\n')
    commit(repo, 'green: implementation')

    acceptance_tests = load_acceptance_tests(repo, red_sha)

    assert 'def test_thing' in acceptance_tests
    assert 'unchanged' not in acceptance_tests


# --- RA-06: `acceptance_tests` is the red commit's diff, not whole-file dumps -----


def test_load_acceptance_tests_holds_only_the_red_commits_added_lines(tmp_path: Path):
    """The red commit adds a new test function to an *existing* test file; only the
    lines it actually added show up, never the file's pre-existing content."""
    repo = make_repo(tmp_path)
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_thing.py').write_text('def test_existing():\n    assert True\n')
    commit(repo, 'base: existing test file')

    (repo / 'tests' / 'test_thing.py').write_text(
        'def test_existing():\n    assert True\n\n\ndef test_new_behaviour():\n    assert 1 == 1\n'
    )
    red_sha = commit(repo, 'red: acceptance test for new behaviour')

    acceptance_tests = load_acceptance_tests(repo, red_sha)

    assert 'def test_new_behaviour' in acceptance_tests
    assert 'assert 1 == 1' in acceptance_tests
    assert 'def test_existing' not in acceptance_tests


def test_load_acceptance_tests_pins_exact_text_for_a_modified_paired_line(tmp_path: Path):
    """Fix round 1 item 1: the red commit *modifies* an existing test function's
    assertion (a paired `-`/`+` line, not a pure addition). The returned text is
    pinned exactly: the replacement `+` line's content, and nothing of the removed
    `-` line. A mutant that also collects `-` lines (e.g. `line.startswith(('+',
    '-'))` instead of just `+`) would put `assert old_value` in the result too --
    this assertion catches that; ledger F-10 asks for the literal, not a substring
    check, precisely so a mutation like that cannot slip through as "well, the new
    line is *also* in there"."""
    repo = make_repo(tmp_path)
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_thing.py').write_text('def test_thing():\n    assert old_value\n')
    commit(repo, 'base: existing assertion')

    (repo / 'tests' / 'test_thing.py').write_text('def test_thing():\n    assert new_value\n')
    red_sha = commit(repo, 'red: modify the existing assertion')

    acceptance_tests = load_acceptance_tests(repo, red_sha)

    assert acceptance_tests == '    assert new_value'
    assert 'old_value' not in acceptance_tests


def test_load_acceptance_tests_drops_fixture_and_init_and_non_python_files(tmp_path: Path):
    """A red commit that adds one test function and also touches a fixture file
    under `tests/fixtures/`, an `__init__.py`, and a non-`.py` data file: only the
    test function's added lines land in `acceptance_tests`."""
    repo = make_repo(tmp_path)
    (repo / 'tests' / 'fixtures').mkdir(parents=True)
    (repo / 'tests' / '__init__.py').write_text('')
    (repo / 'tests' / 'fixtures' / 'sample.json').write_text('{}\n')
    (repo / 'tests' / 'test_new.py').write_text('def test_new_thing():\n    assert True\n')
    red_sha = commit(repo, 'red: adds test, fixture, init and data file all at once')

    acceptance_tests = load_acceptance_tests(repo, red_sha)

    assert 'def test_new_thing' in acceptance_tests
    assert '{}' not in acceptance_tests
    assert 'sample.json' not in acceptance_tests


def test_load_acceptance_tests_root_commit_diffs_against_the_empty_tree(tmp_path: Path):
    """A red commit with no parent (the repo's very first commit) still produces a
    diff -- against the empty tree -- rather than failing on `sha^`."""
    repo = make_repo(tmp_path)
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_root.py').write_text('def test_root_case():\n    assert True\n')
    red_sha = commit(repo, 'red: first commit in the repo is the red commit')

    acceptance_tests = load_acceptance_tests(repo, red_sha)

    assert 'def test_root_case' in acceptance_tests


def test_load_acceptance_tests_omits_files_untouched_by_the_red_commit(tmp_path: Path):
    """A pre-existing test file the red commit never touches contributes nothing,
    even though it exists in the tree at the red commit."""
    repo = make_repo(tmp_path)
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_untouched.py').write_text('def test_untouched():\n    assert True\n')
    commit(repo, 'base: unrelated pre-existing test file')

    (repo / 'tests' / 'test_new.py').write_text('def test_added_by_red():\n    assert True\n')
    red_sha = commit(repo, 'red: only adds a new file')

    acceptance_tests = load_acceptance_tests(repo, red_sha)

    assert 'def test_added_by_red' in acceptance_tests
    assert 'test_untouched' not in acceptance_tests


# --- fix round 1, finding 2: StateError branches ---------------------------------


def test_load_acceptance_tests_with_bogus_red_sha_raises_state_error_naming_the_command(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / 'README.md').write_text('x\n')
    commit(repo, 'init')

    with pytest.raises(StateError) as exc_info:
        load_acceptance_tests(repo, 'not-a-real-sha')

    message = str(exc_info.value)
    assert 'git' in message
    assert 'diff' in message


def test_load_conventions_with_unreadable_agents_md_raises_state_error_not_raw_oserror(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / 'README.md').write_text('x\n')
    commit(repo, 'init')
    # A directory named AGENTS.md: `read_text()` raises `IsADirectoryError` (an
    # `OSError`, not a `FileNotFoundError`), which must come back as `StateError`,
    # never escape raw.
    (repo / 'AGENTS.md').mkdir()

    with pytest.raises(StateError) as exc_info:
        load_conventions(repo)

    assert 'AGENTS.md' in str(exc_info.value)


def test_run_git_with_missing_git_binary_raises_state_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _raise_missing_binary(*args: object, **kwargs: object):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'git'")

    monkeypatch.setattr(state_module.subprocess, 'run', _raise_missing_binary)

    with pytest.raises(StateError) as exc_info:
        load_acceptance_tests(tmp_path, 'deadbeef')

    assert 'git' in str(exc_info.value)
