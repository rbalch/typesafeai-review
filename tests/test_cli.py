"""Acceptance tests for T-01: `ts-review` CLI scaffold and output hygiene.

Covers: --help lists every flag; --calibrate short-circuits before --worktree is
checked; --worktree is enforced as an argparse-style usage error, not a manual
required flag; base-ref fallback order; the no-candidate-ref failure; step 0
deleting only review.md/review.json at the worktree root; and the non-git-dir
failure. Git repos are built with subprocess + tmp_path, per the task's Context
note that there are no fixtures yet.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from typesafe_review.cli import main
from typesafe_review.verdict import EXIT_TOOL_FAILURE

ALL_FLAGS = [
    '--worktree',
    '--task',
    '--red-sha',
    '--base',
    '--dump-state',
    '--record',
    '--replay',
    '--calibrate',
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True)


def make_repo(tmp_path: Path, branch: str = 'main', name: str = 'repo') -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, 'init', '-b', branch)
    _git(repo, 'config', 'user.email', 'test@example.com')
    _git(repo, 'config', 'user.name', 'Test User')
    (repo / 'README.md').write_text('hello\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'initial commit')
    return repo


def test_help_exits_0_and_lists_all_eight_flags(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(['--help'])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ALL_FLAGS:
        assert flag in out, f'{flag} missing from --help output'


def test_calibrate_short_circuits_before_worktree_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(['--calibrate', str(tmp_path / 'x')])
    captured = capsys.readouterr()
    assert rc == EXIT_TOOL_FAILURE
    assert 'not implemented (T-11)' in captured.err
    assert '--worktree' not in captured.err


def test_missing_worktree_is_argparse_style_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert '--worktree' in err


def test_base_fallback_picks_main_when_only_main_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path, branch='main')
    rc = main(['--worktree', str(repo)])
    err = capsys.readouterr().err
    assert rc == EXIT_TOOL_FAILURE
    assert 'main' in err
    assert 'pipeline not implemented' in err


def test_exit_1_when_no_candidate_base_ref_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path, branch='trunk')
    rc = main(['--worktree', str(repo)])
    err = capsys.readouterr().err
    assert rc == EXIT_TOOL_FAILURE
    assert 'develop' in err
    assert 'main' in err
    assert 'master' in err


def test_stale_outputs_removed_sibling_untouched(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, branch='main')
    (repo / 'review.md').write_text('stale md')
    (repo / 'review.json').write_text('{"stale": true}')
    (repo / 'review.txt').write_text('leave me alone')

    rc = main(['--worktree', str(repo)])

    assert rc == EXIT_TOOL_FAILURE
    assert not (repo / 'review.md').exists()
    assert not (repo / 'review.json').exists()
    assert (repo / 'review.txt').exists()
    assert (repo / 'review.txt').read_text() == 'leave me alone'


def test_non_git_dir_exits_1(tmp_path: Path) -> None:
    plain_dir = tmp_path / 'not-a-repo'
    plain_dir.mkdir()
    rc = main(['--worktree', str(plain_dir)])
    assert rc == EXIT_TOOL_FAILURE


def test_worktree_subdirectory_is_rejected_not_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path, branch='main')
    sub = repo / 'sub'
    sub.mkdir()
    (sub / 'review.md').write_text('should not be touched')

    rc = main(['--worktree', str(sub)])
    err = capsys.readouterr().err

    assert rc == EXIT_TOOL_FAILURE
    assert 'not a worktree root' in err
    assert (sub / 'review.md').exists()
