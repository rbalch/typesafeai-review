"""Acceptance tests for the deterministic checks (spec §4.1).

Each fixture is a throwaway git repo with a tiny pytest suite (a `pyproject.toml`
carrying `pytest` as a dev dependency with `[tool.uv] package = false`, so
`uv run pytest -q` behaves there as it would in a real target repo). Every test
builds its own repo under `tmp_path`; nothing here touches the caller's worktree.

`pkg.py`, at each fixture repo's root, plays the part of "the implementation": the
acceptance test (`tests/test_thing.py`) imports it and never changes between red and
green, so a diff on `tests/` only ever means the *test* moved, not the code under test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import typesafe_review.checks as checks_module
from typesafe_review.checks import CheckError, redact, run_checks

_PYPROJECT = """\
[project]
name = "fixture"
version = "0.0.0"
requires-python = ">=3.10"

[tool.uv]
package = false

[dependency-groups]
dev = ["pytest"]

[tool.pytest.ini_options]
pythonpath = ["."]
"""

_PKG_BROKEN = 'def thing():\n    return False\n'
_PKG_FIXED = 'def thing():\n    return True\n'
_TEST_THING = 'from pkg import thing\n\n\ndef test_thing():\n    assert thing()\n'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / 'pyproject.toml').write_text(_PYPROJECT)
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_thing.py').write_text(_TEST_THING)
    _git(repo, 'init', '-q')
    _git(repo, 'config', 'user.email', 'test@example.com')
    _git(repo, 'config', 'user.name', 'Test')


def _commit(repo: Path, message: str) -> str:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', message)
    out = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, check=True, capture_output=True)
    return out.stdout.decode().strip()


def _result(report, name: str):
    return next(r for r in report.results if r.name == name)


def _findings(report, finding_id: str):
    return [f for f in report.findings if f.id == finding_id]


def test_red_commit_failing_then_fixed_is_a_passing_red_proof(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_BROKEN)
    red_sha = _commit(repo, 'red: acceptance test, no implementation yet')
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    _commit(repo, 'green: implementation')

    report = run_checks(repo, red_sha, timeout=60)

    red = _result(report, 'red proof')
    assert red.status == 'pass'
    assert not _findings(report, 'red_proof_missing')


def test_red_commit_that_already_passes_fails_red_proof(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    red_sha = _commit(repo, 'red: acceptance test (implementation already there)')
    (repo / 'README.md').write_text('noop\n')
    _commit(repo, 'green: unrelated change')

    report = run_checks(repo, red_sha, timeout=60)

    red = _result(report, 'red proof')
    assert red.status == 'fail'
    assert _findings(report, 'red_proof_missing')


def test_no_red_sha_is_not_run_with_a_finding(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    _commit(repo, 'only commit')

    report = run_checks(repo, None, timeout=60)

    red = _result(report, 'red proof')
    assert red.status == 'not_run'
    assert _findings(report, 'red_proof_missing')


def test_acceptance_test_file_edited_after_red_is_flagged(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_BROKEN)
    red_sha = _commit(repo, 'red: acceptance test, no implementation yet')
    # The builder loosens the acceptance test instead of fixing the code.
    (repo / 'tests' / 'test_thing.py').write_text(
        'from pkg import thing\n\n\ndef test_thing():\n    assert thing() or True\n'
    )
    (repo / 'pkg.py').write_text(_PKG_BROKEN)
    _commit(repo, 'green: edited the acceptance test instead of the code')

    report = run_checks(repo, red_sha, timeout=60)

    edited = _findings(report, 'acceptance_tests_edited')
    assert edited
    assert edited[0].path == 'tests/test_thing.py'


def test_new_test_file_added_after_red_is_not_flagged(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_BROKEN)
    red_sha = _commit(repo, 'red: acceptance test, no implementation yet')
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    (repo / 'tests' / 'test_extra.py').write_text('def test_extra():\n    assert True\n')
    _commit(repo, 'green: implementation plus a new test file')

    report = run_checks(repo, red_sha, timeout=60)

    assert not _findings(report, 'acceptance_tests_edited')


def test_no_makefile_is_not_applicable(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    _commit(repo, 'only commit')

    report = run_checks(repo, None, timeout=60)

    make = _result(report, 'make check')
    assert make.status == 'not_applicable'


def test_failing_make_check_fails_with_notes(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    (repo / 'Makefile').write_text('check:\n\t@echo boom-message-for-notes && exit 1\n')
    _commit(repo, 'only commit')

    report = run_checks(repo, None, timeout=60)

    make = _result(report, 'make check')
    assert make.status == 'fail'
    assert 'boom-message-for-notes' in make.notes
    assert _findings(report, 'gate_failed')


def test_subprocess_exceeding_injected_timeout_fails_with_notes(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    (repo / 'tests' / 'test_slow.py').write_text(
        'import time\n\ndef test_slow():\n    time.sleep(2)\n    assert True\n'
    )
    _commit(repo, 'only commit')

    report = run_checks(repo, None, timeout=1)

    green = _result(report, 'green at HEAD')
    assert green.status == 'fail'
    assert green.notes == 'timed out after 1s'


def test_secret_like_output_is_redacted(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    (repo / 'Makefile').write_text('check:\n\t@echo token=abc123 && exit 1\n')
    _commit(repo, 'only commit')

    report = run_checks(repo, None, timeout=60)

    make = _result(report, 'make check')
    assert 'token=abc123' not in make.notes
    assert '<redacted>' in make.notes


def test_redact_strips_url_embedded_credentials() -> None:
    assert redact('https://u:p@h/x') == 'https://<redacted>@h/x'


def test_redact_leaves_a_bare_ssh_username_alone() -> None:
    # RA-04 fix round 2: `ssh://git@host/...` has no password component -- only a
    # `user:pass@` pair is a credential worth redacting, not a bare `user@host`.
    assert redact('ssh://git@h/x') == 'ssh://git@h/x'


def test_redact_still_strips_token_shaped_text() -> None:
    # existing redact behaviour unchanged by the new URL pattern.
    assert redact('token=abc123') == '<redacted>'
    assert 'abc123' not in redact('token=abc123')


def test_temp_checkout_is_removed_even_when_pytest_fails(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_BROKEN)
    red_sha = _commit(repo, 'red: acceptance test, no implementation yet')
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    _commit(repo, 'green: implementation')

    run_checks(repo, red_sha, timeout=60)

    out = subprocess.run(['git', 'worktree', 'list'], cwd=repo, check=True, capture_output=True).stdout.decode()
    assert len(out.strip().splitlines()) == 1


def test_red_commit_with_no_tests_collected_fails_red_proof(tmp_path: Path) -> None:
    """pytest exit 5 (no tests collected) is not evidence of anything red."""
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'tests' / 'test_thing.py').unlink()
    (repo / 'pkg.py').write_text(_PKG_BROKEN)
    red_sha = _commit(repo, 'red: empty tests directory')
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    _commit(repo, 'green: implementation')

    report = run_checks(repo, red_sha, timeout=60)

    red = _result(report, 'red proof')
    assert red.status == 'fail'
    assert 'pytest exit 5' in red.notes
    assert 'no tests collected' in red.notes
    finding = _findings(report, 'red_proof_missing')
    assert finding
    assert 'pytest exit 5' in finding[0].notes


def test_red_commit_with_collection_error_is_a_passing_red_proof(tmp_path: Path) -> None:
    """pytest exit 2 (collection error / interrupted run) counts as red."""
    repo = tmp_path / 'repo'
    _init_repo(repo)
    # No pkg.py at all at the red commit: the acceptance test's import fails at
    # collection time, exit code 2, before any implementation exists.
    red_sha = _commit(repo, 'red: acceptance test, module does not exist yet')
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    _commit(repo, 'green: implementation')

    report = run_checks(repo, red_sha, timeout=60)

    red = _result(report, 'red proof')
    assert red.status == 'pass'
    assert not _findings(report, 'red_proof_missing')


def test_green_at_head_fails_when_it_cannot_verify_the_acceptance_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_BROKEN)
    red_sha = _commit(repo, 'red: acceptance test, no implementation yet')
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    _commit(repo, 'green: implementation')

    real_git = checks_module._git

    def flaky_git(worktree, args, timeout):
        if args and args[0] == 'diff':
            raise CheckError('git diff exploded')
        return real_git(worktree, args, timeout)

    monkeypatch.setattr(checks_module, '_git', flaky_git)

    report = run_checks(repo, red_sha, timeout=60)

    green = _result(report, 'green at HEAD')
    assert green.status == 'fail'
    assert green.notes.startswith('could not verify acceptance tests:')
    finding = _findings(report, 'gate_failed')
    assert finding
    assert finding[0].notes.startswith('could not verify acceptance tests:')


@pytest.mark.skipif(os.geteuid() == 0, reason='root ignores file permissions')
def test_unreadable_makefile_fails_with_notes(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _init_repo(repo)
    (repo / 'pkg.py').write_text(_PKG_FIXED)
    makefile = repo / 'Makefile'
    makefile.write_text('check:\n\t@echo ok\n')
    _commit(repo, 'only commit')
    makefile.chmod(0o000)

    try:
        report = run_checks(repo, None, timeout=60)
    finally:
        makefile.chmod(0o644)

    make = _result(report, 'make check')
    assert make.status == 'fail'
    assert 'could not read Makefile' in make.notes
    assert _findings(report, 'gate_failed')
