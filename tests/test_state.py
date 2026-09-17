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


# --- fix round 1, finding 2: StateError branches ---------------------------------


def test_load_acceptance_tests_with_bogus_red_sha_raises_state_error_naming_the_command(tmp_path: Path):
    repo = make_repo(tmp_path)
    (repo / 'README.md').write_text('x\n')
    commit(repo, 'init')

    with pytest.raises(StateError) as exc_info:
        load_acceptance_tests(repo, 'not-a-real-sha')

    message = str(exc_info.value)
    assert 'git' in message
    assert 'ls-tree' in message


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
