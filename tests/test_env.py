"""Acceptance tests for RA-01: `.env` loading and `--doctor`.

One test per acceptance clause in `tasks/run-anywhere/RA-01-env-and-doctor.md`:
search order (`--env-file` -> `<worktree>/.env` -> cwd walk -> `~/.config/typesafe-review/env`,
first hit per key wins, every file consulted), a process env var never overridden,
non-`TYPESAFE_` keys ignored, quotes/`export` parsed, the `/v1/systemone` suffix
stripped with a stderr note, a missing `--env-file` exiting 1, and the two `--doctor`
CLI behaviours (via `cli.main`, per the task): a successful replay ending in `ok`, and
a missing key with no env file naming all four search locations.

`conftest.py`'s autouse fixture keeps `HOME`/cwd inside a throwaway `tmp_path` and
strips any `TYPESAFE_*` variable the shell already had, so nothing here can reach a
real credential by accident; where a test needs full control over the search inputs
it still passes `home=`/`cwd=` explicitly to `load_env`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import typesafe_review.cli as cli_module
from typesafe_review import pipeline
from typesafe_review.ask import AskFailed, request_key
from typesafe_review.cli import DOCTOR_QUESTIONS, DOCTOR_STATE, main
from typesafe_review.env import EnvError, load_env
from typesafe_review.verdict import EXIT_APPROVE, EXIT_TOOL_FAILURE

FIXTURES_DOCTOR = Path(__file__).resolve().parent / 'fixtures' / 'responses' / 'doctor'


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# Search order: first hit per key wins, every file is consulted.
# ---------------------------------------------------------------------------


def test_env_file_flag_takes_precedence_over_every_other_source(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'cwd'
    worktree = tmp_path / 'worktree'
    explicit = _write(tmp_path / 'explicit.env', 'TYPESAFE_API_KEY=from-env-file\n')
    _write(worktree / '.env', 'TYPESAFE_API_KEY=from-worktree\n')
    _write(cwd / '.env', 'TYPESAFE_API_KEY=from-cwd\n')
    _write(home / '.config' / 'typesafe-review' / 'env', 'TYPESAFE_API_KEY=from-home\n')

    load_env(worktree, explicit, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_API_KEY'] == 'from-env-file'


def test_worktree_env_beats_cwd_walk_and_home_config(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'cwd'
    worktree = tmp_path / 'worktree'
    _write(worktree / '.env', 'TYPESAFE_API_KEY=from-worktree\n')
    _write(cwd / '.env', 'TYPESAFE_API_KEY=from-cwd\n')
    _write(home / '.config' / 'typesafe-review' / 'env', 'TYPESAFE_API_KEY=from-home\n')

    load_env(worktree, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_API_KEY'] == 'from-worktree'


def test_cwd_walk_beats_home_config_and_walks_up_to_root(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'a' / 'b' / 'c'
    cwd.mkdir(parents=True)
    # a `.env` two levels above cwd must still be found by the walk.
    _write(tmp_path / 'a' / '.env', 'TYPESAFE_API_KEY=from-ancestor\n')
    _write(home / '.config' / 'typesafe-review' / 'env', 'TYPESAFE_API_KEY=from-home\n')

    load_env(None, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_API_KEY'] == 'from-ancestor'


def test_home_config_used_when_nothing_else_provides_the_key(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    cwd = tmp_path / 'cwd'
    cwd.mkdir(parents=True)
    _write(home / '.config' / 'typesafe-review' / 'env', 'TYPESAFE_API_KEY=from-home\n')

    load_env(None, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_API_KEY'] == 'from-home'


def test_all_files_are_consulted_not_just_the_first_hit(tmp_path: Path) -> None:
    """Two files with disjoint keys must both contribute -- the search never stops
    at the first file that defines *any* key."""
    home = tmp_path / 'home'
    worktree = tmp_path / 'worktree'
    cwd = tmp_path / 'cwd'
    cwd.mkdir(parents=True)
    _write(worktree / '.env', 'TYPESAFE_API_KEY=from-worktree\n')
    _write(home / '.config' / 'typesafe-review' / 'env', 'TYPESAFE_DEFAULT_MODEL=from-home\n')

    contributing = load_env(worktree, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_API_KEY'] == 'from-worktree'
    assert os.environ['TYPESAFE_DEFAULT_MODEL'] == 'from-home'
    assert (worktree / '.env') in contributing
    assert (home / '.config' / 'typesafe-review' / 'env') in contributing


def test_non_contributing_file_is_excluded_from_the_return_value(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    worktree = tmp_path / 'worktree'
    cwd = tmp_path / 'cwd'
    cwd.mkdir(parents=True)
    _write(worktree / '.env', 'TYPESAFE_API_KEY=from-worktree\n')
    # This file's only key is shadowed by the worktree file above, so it never
    # contributes and must not appear in the returned list.
    _write(home / '.config' / 'typesafe-review' / 'env', 'TYPESAFE_API_KEY=from-home\n')

    contributing = load_env(worktree, None, home=home, cwd=cwd)

    assert (worktree / '.env') in contributing
    assert (home / '.config' / 'typesafe-review' / 'env') not in contributing


# ---------------------------------------------------------------------------
# Never override a variable already set in the process environment.
# ---------------------------------------------------------------------------


def test_a_set_process_variable_is_never_overridden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('TYPESAFE_API_KEY', 'already-set')
    home = tmp_path / 'home'
    worktree = tmp_path / 'worktree'
    cwd = tmp_path / 'cwd'
    cwd.mkdir(parents=True)
    _write(worktree / '.env', 'TYPESAFE_API_KEY=from-worktree\n')

    contributing = load_env(worktree, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_API_KEY'] == 'already-set'
    # the file's only key was already set, so it never contributed.
    assert contributing == []


# ---------------------------------------------------------------------------
# Non-`TYPESAFE_` keys ignored; quotes and `export` parsed.
# ---------------------------------------------------------------------------


def test_non_typesafe_keys_are_ignored(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    worktree = tmp_path / 'worktree'
    cwd = tmp_path / 'cwd'
    cwd.mkdir(parents=True)
    _write(worktree / '.env', 'OTHER_TOOL_KEY=nope\nTYPESAFE_API_KEY=yes\n')

    load_env(worktree, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_API_KEY'] == 'yes'
    assert 'OTHER_TOOL_KEY' not in os.environ


def test_quotes_and_export_and_comments_are_parsed(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    worktree = tmp_path / 'worktree'
    cwd = tmp_path / 'cwd'
    cwd.mkdir(parents=True)
    _write(
        worktree / '.env',
        '# a comment line\n'
        '\n'
        'export TYPESAFE_API_KEY="double-quoted"\n'
        "TYPESAFE_DEFAULT_MODEL='single-quoted'\n"
        'TYPESAFE_BASE_URL=unquoted-value\n',
    )

    load_env(worktree, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_API_KEY'] == 'double-quoted'
    assert os.environ['TYPESAFE_DEFAULT_MODEL'] == 'single-quoted'
    assert os.environ['TYPESAFE_BASE_URL'] == 'unquoted-value'


# ---------------------------------------------------------------------------
# `/v1/systemone` stripped from `TYPESAFE_BASE_URL`, with a stderr note.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'raw',
    [
        'https://api.typesafe.ai/v1/systemone',
        'https://api.typesafe.ai/v1/systemone/',
    ],
)
def test_base_url_v1_systemone_suffix_is_stripped_with_a_stderr_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    home = tmp_path / 'home'
    worktree = tmp_path / 'worktree'
    cwd = tmp_path / 'cwd'
    cwd.mkdir(parents=True)
    _write(worktree / '.env', f'TYPESAFE_BASE_URL={raw}\n')

    load_env(worktree, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_BASE_URL'] == 'https://api.typesafe.ai'
    err = capsys.readouterr().err
    assert 'stripped' in err
    assert 'TYPESAFE_BASE_URL' in err


def test_base_url_without_the_suffix_is_left_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / 'home'
    worktree = tmp_path / 'worktree'
    cwd = tmp_path / 'cwd'
    cwd.mkdir(parents=True)
    _write(worktree / '.env', 'TYPESAFE_BASE_URL=https://api.typesafe.ai\n')

    load_env(worktree, None, home=home, cwd=cwd)

    import os

    assert os.environ['TYPESAFE_BASE_URL'] == 'https://api.typesafe.ai'
    assert 'stripped' not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# `--env-file` missing -> exit 1.
# ---------------------------------------------------------------------------


def test_missing_env_file_raises_env_error(tmp_path: Path) -> None:
    missing = tmp_path / 'does-not-exist.env'
    with pytest.raises(EnvError):
        load_env(None, missing, home=tmp_path / 'home', cwd=tmp_path / 'cwd')


def test_cli_exits_1_when_env_file_flag_names_a_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / 'does-not-exist.env'

    rc = main(['--doctor', '--env-file', str(missing)])

    assert rc == EXIT_TOOL_FAILURE
    assert str(missing) in capsys.readouterr().err


# ---------------------------------------------------------------------------
# `--doctor`, via `cli.main`.
# ---------------------------------------------------------------------------


def test_doctor_replay_exits_0_and_stdout_ends_with_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv('TYPESAFE_API_KEY', 'dummy-for-replay')

    rc = main(['--doctor', '--replay', str(FIXTURES_DOCTOR)])

    out = capsys.readouterr().out
    assert rc == EXIT_APPROVE
    assert out.rstrip('\n').endswith('ok')


def test_doctor_missing_key_and_no_env_file_names_all_four_locations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `conftest.py`'s autouse fixture already isolates HOME/cwd and strips every
    # `TYPESAFE_*` variable, and no `--worktree`/`--env-file` is given here, so all
    # four search locations come up empty.
    rc = main(['--doctor'])

    err = capsys.readouterr().err
    assert rc == EXIT_TOOL_FAILURE
    assert 'skipped: --env-file (not given)' in err
    assert 'skipped: worktree/.env (not given)' in err
    assert '.env walk from' in err
    assert '.config/typesafe-review/env' in err


def test_doctor_verifies_the_answer_not_just_survival_of_the_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `--doctor` that only checked the call survived (`ask_all` didn't raise)
    would print `ok` for any answer, forged or not. Fix-round-1: `ask_all` raising
    `AskFailed` must exit 1 with no `ok` on stdout, the failure named, and the four
    search locations (same shape as the missing-key path)."""
    monkeypatch.setenv('TYPESAFE_API_KEY', 'dummy-for-ask-failure')

    async def _raise(*args: object, **kwargs: object) -> None:
        raise AskFailed('doctor', RuntimeError('boom'))

    monkeypatch.setattr(cli_module, 'ask_all', _raise)

    rc = main(['--doctor'])

    out = capsys.readouterr()
    assert rc == EXIT_TOOL_FAILURE
    assert out.out == ''
    assert 'boom' in out.err
    assert 'skipped: --env-file (not given)' in out.err
    assert 'skipped: worktree/.env (not given)' in out.err
    assert '.env walk from' in out.err
    assert '.config/typesafe-review/env' in out.err


def test_doctor_replay_with_a_low_noul_exits_1_without_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A forged/low-confidence replay (`noul: 0.02`) must not print `ok`: the
    fixture file is planted under `tmp_path`, keyed with the exact same
    `ask.request_key` a real `--record` pass would have used for `DOCTOR_STATE`/
    `DOCTOR_QUESTIONS`, so `--doctor --replay` finds it and has to judge the
    answer itself rather than just surviving the lookup."""
    monkeypatch.setenv('TYPESAFE_API_KEY', 'dummy-for-low-noul')
    model = pipeline.resolve_model()
    key = request_key(DOCTOR_STATE, DOCTOR_QUESTIONS, model)
    responses_dir = tmp_path / 'responses'
    responses_dir.mkdir()
    body = json.dumps(
        {
            'model': model,
            'answers': {'check': {'type': 'noul', 'noul': 0.02}},
            'usage': {'input_tokens': 5, 'output_tokens': 2},
        }
    )
    (responses_dir / f'{key}.json').write_text(body)

    rc = main(['--doctor', '--replay', str(responses_dir)])

    out = capsys.readouterr()
    assert rc == EXIT_TOOL_FAILURE
    assert out.out == ''
    assert 'check question answered 0.02, expected >= 0.5' in out.err
