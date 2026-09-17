"""Acceptance tests for T-01: `ts-review` CLI scaffold and output hygiene.

Covers: --help lists every flag; --calibrate short-circuits before --worktree is
checked; --worktree is enforced as an argparse-style usage error, not a manual
required flag; base-ref fallback order; the no-candidate-ref failure; step 0
deleting only review.md/review.json at the worktree root; and the non-git-dir
failure. Git repos are built with subprocess + tmp_path, per the task's Context
note that there are no fixtures yet.
"""

from __future__ import annotations

import json
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
    assert 'base: main' in err
    # base resolved to 'main' with no other ref to diff against (base == HEAD, no
    # task, no red sha): the pipeline (T-10) runs for real past this point and fails
    # for a reason of its own -- here, no API key configured -- never the T-01
    # placeholder this test used to pin.
    assert 'checks…' in err


def test_exit_1_when_no_candidate_base_ref_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path, branch='trunk')
    rc = main(['--worktree', str(repo)])
    err = capsys.readouterr().err
    assert rc == EXIT_TOOL_FAILURE
    assert 'develop' in err
    assert 'main' in err
    assert 'master' in err


def test_stale_outputs_removed_sibling_untouched(tmp_path: Path) -> None:
    """Step 0 deletes only `ts-review.md` / `ts-review.json`; the LLM reviewer's
    `review.md` / `review.json` (distinct names, spec §3/§9) and an unrelated
    `review.txt` sibling are left alone.
    """
    repo = make_repo(tmp_path, branch='main')
    (repo / 'ts-review.md').write_text('stale md')
    (repo / 'ts-review.json').write_text('{"stale": true}')
    (repo / 'review.md').write_text('llm reviewer md')
    (repo / 'review.json').write_text('{"llm_reviewer": true}')
    (repo / 'review.txt').write_text('leave me alone')

    rc = main(['--worktree', str(repo)])

    assert rc == EXIT_TOOL_FAILURE
    assert not (repo / 'ts-review.md').exists()
    assert not (repo / 'ts-review.json').exists()
    assert (repo / 'review.md').exists()
    assert (repo / 'review.md').read_text() == 'llm reviewer md'
    assert (repo / 'review.json').exists()
    assert (repo / 'review.json').read_text() == '{"llm_reviewer": true}'
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


# --- T-05: --dump-state ---------------------------------------------------------

_TASK_FILE = """\
---
id: T-42
title: Example task
---

## Acceptance

- first criterion
- second criterion
"""


def _git_status_short(repo: Path) -> str:
    result = subprocess.run(
        ['git', '-C', str(repo), 'status', '--short'],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_dump_state_writes_n_plus_one_files_and_touches_nothing_in_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path, branch='main')
    (repo / 'pkg.py').write_text('def f():\n    return 1\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'base')
    (repo / 'pkg.py').write_text('def f():\n    return 2\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'change')

    dump_dir = tmp_path / 'dump'
    rc = main(['--worktree', str(repo), '--base', 'HEAD~1', '--dump-state', str(dump_dir)])
    err = capsys.readouterr().err

    assert rc == 0
    written = sorted(p.name for p in dump_dir.iterdir())
    assert written == ['change.json', 'hunk-01.json']  # one hunk -> N+1 files
    assert 'tokens' in err
    assert _git_status_short(repo) == ''


def test_dump_state_with_task_populates_task_block(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, branch='main')
    (repo / 'pkg.py').write_text('def f():\n    return 1\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'base')
    (repo / 'pkg.py').write_text('def f():\n    return 2\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'change')

    task_path = tmp_path / 'T-42-example.md'
    task_path.write_text(_TASK_FILE)

    dump_dir = tmp_path / 'dump'
    rc = main(['--worktree', str(repo), '--base', 'HEAD~1', '--task', str(task_path), '--dump-state', str(dump_dir)])

    assert rc == 0
    change_state = json.loads((dump_dir / 'change.json').read_text())
    assert change_state['task'] == {
        'id': 'T-42',
        'title': 'Example task',
        'acceptance': ['first criterion', 'second criterion'],
        'criteria_text': '\n- first criterion\n- second criterion\n',
    }
    hunk_files = sorted(dump_dir.glob('hunk-*.json'))
    assert hunk_files
    hunk_state = json.loads(hunk_files[0].read_text())
    assert hunk_state['task'] == {
        'id': 'T-42',
        'title': 'Example task',
        'acceptance': ['first criterion', 'second criterion'],
    }


def test_dump_state_with_red_sha_populates_acceptance_tests(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, branch='main')
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_thing.py').write_text('def test_thing():\n    assert True\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'red: acceptance test')
    red_sha = subprocess.run(
        ['git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / 'pkg.py').write_text('def f():\n    return 1\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'green: implementation')

    dump_dir = tmp_path / 'dump'
    rc = main(['--worktree', str(repo), '--base', 'HEAD~1', '--red-sha', red_sha, '--dump-state', str(dump_dir)])

    assert rc == 0
    change_state = json.loads((dump_dir / 'change.json').read_text())
    assert 'def test_thing' in change_state['acceptance_tests']


def test_dump_state_without_task_or_red_sha_has_null_task_and_empty_acceptance_tests(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, branch='main')
    (repo / 'pkg.py').write_text('def f():\n    return 1\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'base')
    (repo / 'pkg.py').write_text('def f():\n    return 2\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'change')

    dump_dir = tmp_path / 'dump'
    rc = main(['--worktree', str(repo), '--base', 'HEAD~1', '--dump-state', str(dump_dir)])

    assert rc == 0
    change_state = json.loads((dump_dir / 'change.json').read_text())
    assert change_state['task'] is None
    assert change_state['acceptance_tests'] == ''


def test_dump_state_does_not_delete_preexisting_review_outputs(tmp_path: Path) -> None:
    """Fix round 1, finding 1: dump mode is read-only inspection of the target; it
    must never run step 0's stale-output cleanup, or planting `review.md` /
    `review.json` in a target that was never actually reviewed would lose them.
    """
    repo = make_repo(tmp_path, branch='main')
    (repo / 'pkg.py').write_text('def f():\n    return 1\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'base')
    (repo / 'pkg.py').write_text('def f():\n    return 2\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'change')

    (repo / 'review.md').write_text('pre-existing review\n')
    (repo / 'review.json').write_text('{"pre_existing": true}\n')
    status_before = _git_status_short(repo)

    dump_dir = tmp_path / 'dump'
    rc = main(['--worktree', str(repo), '--base', 'HEAD~1', '--dump-state', str(dump_dir)])

    assert rc == 0
    assert (repo / 'review.md').exists()
    assert (repo / 'review.md').read_text() == 'pre-existing review\n'
    assert (repo / 'review.json').exists()
    assert (repo / 'review.json').read_text() == '{"pre_existing": true}\n'
    assert _git_status_short(repo) == status_before


def test_dump_state_write_failure_names_the_path_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fix round 1, finding 3: a write failure while dumping state is reported, not
    left to raise an uncaught `OSError`.
    """
    repo = make_repo(tmp_path, branch='main')
    (repo / 'pkg.py').write_text('def f():\n    return 1\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'base')
    (repo / 'pkg.py').write_text('def f():\n    return 2\n')
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-m', 'change')

    # A regular file where the dump dir should be: `mkdir(parents=True, exist_ok=True)`
    # cannot create a directory there.
    dump_target = tmp_path / 'dump-is-a-file'
    dump_target.write_text('not a directory')

    rc = main(['--worktree', str(repo), '--base', 'HEAD~1', '--dump-state', str(dump_target)])
    err = capsys.readouterr().err

    assert rc == EXIT_TOOL_FAILURE
    assert str(dump_target) in err
