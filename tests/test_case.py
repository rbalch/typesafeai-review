"""Acceptance tests for RA-04: `--case` writes a real-run fixture.

One test per acceptance clause in `tasks/run-anywhere/RA-04-real-run-cases.md`:
the replayed sample run with `--case <tmp>` writes every file the task names;
`keys.json` keys equal the `hunk_key` of every sliced hunk plus `<change>`;
secret-shaped strings in state are redacted; `--case` with `--record` is an
argparse error.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from typesafe_review import case
from typesafe_review.cli import main
from typesafe_review.questions import expected_change_questions, expected_hunk_questions
from typesafe_review.slicing import slice_diff
from typesafe_review.state import ChangeState, HunkState
from typesafe_review.verdict import EXIT_CHANGES_REQUESTED

_BUILD_REPO_PATH = Path(__file__).parent / 'fixtures' / 'repos' / 'sample' / 'build_repo.py'
_REPLAY_DIR = Path(__file__).parent / 'fixtures' / 'responses' / 'pipeline_sample'


def _load_build_repo():
    spec = importlib.util.spec_from_file_location('sample_build_repo_case', _BUILD_REPO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['sample_build_repo_case'] = module
    spec.loader.exec_module(module)
    return module


_build_repo = _load_build_repo()


@pytest.fixture()
def sample(tmp_path: Path):
    return _build_repo.build(tmp_path / 'sample')


# ---------------------------------------------------------------------------
# 1. `--case <tmp>` against the replayed sample run writes every file the task
#    names, and `keys.json`'s keys equal every sliced hunk's `hunk_key` plus
#    `<change>`.
# ---------------------------------------------------------------------------


def test_case_writes_state_responses_outputs_and_meta(sample, tmp_path: Path) -> None:
    case_dir = tmp_path / 'case1'

    rc = main(
        [
            '--worktree',
            str(sample.repo),
            '--task',
            str(sample.task),
            '--red-sha',
            sample.red_sha,
            '--base',
            sample.base,
            '--replay',
            str(_REPLAY_DIR),
            '--case',
            str(case_dir),
        ]
    )
    assert rc == EXIT_CHANGES_REQUESTED

    state_dir = case_dir / 'state'
    assert sorted(p.name for p in state_dir.iterdir()) == ['change.json', 'hunk-01.json', 'hunk-02.json', 'keys.json']

    # Independently sliced (never trusting the run's own output): every hunk's
    # `f'{path}@{header}'` plus `<change>` is exactly `keys.json`'s key set.
    change = slice_diff(sample.repo, sample.base)
    expected_keys = {f'{hunk.path}@{hunk.header}' for hunk in change.hunks} | {case.CHANGE_KEY}

    keys = json.loads((state_dir / 'keys.json').read_text())
    assert set(keys) == expected_keys
    for info in keys.values():
        assert info['state'] in ('hunk-01.json', 'hunk-02.json', 'change.json')
        assert isinstance(info['questions'], list) and info['questions']
        assert isinstance(info['request_key'], str) and len(info['request_key']) == 64

    # responses/ was filled by mirroring the replay hits -- exactly the recorded
    # fixture's own three files, since the state + questions + model this run sent
    # are identical to the ones those fixtures were recorded under.
    responses_dir = case_dir / 'responses'
    assert sorted(p.name for p in responses_dir.iterdir()) == sorted(p.name for p in _REPLAY_DIR.iterdir())

    # outputs are copies of the run's own ts-review.md / .json.
    worktree_md = (sample.repo / 'ts-review.md').read_text()
    worktree_json = json.loads((sample.repo / 'ts-review.json').read_text())
    assert (case_dir / 'ts-review.md').read_text() == worktree_md
    assert json.loads((case_dir / 'ts-review.json').read_text()) == worktree_json

    meta = json.loads((case_dir / 'meta.json').read_text())
    for field in ('repo', 'base', 'head', 'task_source', 'model', 'date', 'verdict', 'score'):
        assert field in meta
    assert meta['task_source'] == 'file'
    assert meta['verdict'] == 'CHANGES_REQUESTED'
    # `base` is the resolved base *sha* (RA-03's `target.base_sha`), not the ref
    # name `--base` was given -- matches `ts-review.json`'s own `base` field.
    assert meta['base'] == worktree_json['base']


# ---------------------------------------------------------------------------
# 1b. `meta.json`'s `repo` field never carries a credentialed origin's userinfo
#     (RA-04 fix round 1, HIGH).
# ---------------------------------------------------------------------------

_FAKE_ORIGIN_USER = 'gituser'
_FAKE_ORIGIN_TOKEN = 'ghp_SYNTHETIC_NOT_A_REAL_TOKEN_0123456789'  # synthetic, test-only


def _run_case_with_origin(sample, case_dir: Path, origin_url: str) -> dict:
    subprocess.run(
        ['git', '-C', str(sample.repo), 'remote', 'add', 'origin', origin_url],
        check=True,
        capture_output=True,
    )

    rc = main(
        [
            '--worktree',
            str(sample.repo),
            '--task',
            str(sample.task),
            '--red-sha',
            sample.red_sha,
            '--base',
            sample.base,
            '--replay',
            str(_REPLAY_DIR),
            '--case',
            str(case_dir),
        ]
    )
    assert rc == EXIT_CHANGES_REQUESTED
    return json.loads((case_dir / 'meta.json').read_text())


def test_meta_json_never_carries_a_credentialed_origin_url(sample, tmp_path: Path) -> None:
    case_dir = tmp_path / 'case-credentialed-origin'
    meta = _run_case_with_origin(
        sample, case_dir, f'https://{_FAKE_ORIGIN_USER}:{_FAKE_ORIGIN_TOKEN}@example.com/o/r.git'
    )
    meta_text = json.dumps(meta)
    assert _FAKE_ORIGIN_USER not in meta_text
    assert _FAKE_ORIGIN_TOKEN not in meta_text
    # `pipeline._repo_identity` strips the userinfo outright before `meta` is ever
    # built (`checks.redact`'s own `://user:pass@` -> `://<redacted>@` pattern is
    # the second, independent guard `write_case` applies -- it never fires here
    # because there is nothing left for it to catch).
    assert meta['repo'] == 'https://example.com/o/r.git'


def test_meta_json_leaves_an_ssh_origin_unchanged(sample, tmp_path: Path) -> None:
    # RA-04 fix round 2: `ssh://git@host/...` has no password -- a bare username is
    # not a credential, and redacting it would make a committed fixture's
    # `meta.json` factually wrong about what the remote actually is.
    case_dir = tmp_path / 'case-ssh-origin'
    origin = 'ssh://git@example.com/o/r.git'
    meta = _run_case_with_origin(sample, case_dir, origin)
    assert meta['repo'] == origin


def test_meta_json_leaves_a_scp_like_origin_unchanged(sample, tmp_path: Path) -> None:
    # The scp-like ssh form has no `://` at all, so neither `_strip_url_userinfo`
    # (no http(s) scheme) nor `_URL_CREDENTIAL_RE` (no `://`) touch it.
    case_dir = tmp_path / 'case-scp-origin'
    origin = 'git@example.com:o/r.git'
    meta = _run_case_with_origin(sample, case_dir, origin)
    assert meta['repo'] == origin


# ---------------------------------------------------------------------------
# 2. Secret-shaped strings in state are redacted (synthetic, never a real value).
# ---------------------------------------------------------------------------

_SECRET = 'token=SYNTHETIC_NOT_A_REAL_SECRET_0123456789'


def test_write_case_redacts_secret_shaped_strings(tmp_path: Path) -> None:
    hunk_state: HunkState = {
        'task': None,
        'file': {'path': 'pkg.py', 'language': 'python', 'is_test': False, 'is_new': False},
        'hunk': {'header': '@@ -1 +1 @@', 'diff': f'-old\n+new  {_SECRET}\n', 'after': f'new  {_SECRET}\n'},
        'neighbours': {'same_module_helpers': [], 'tests_touching_file': []},
        'conventions': '',
    }
    change_state: ChangeState = {
        'task': None,
        'diff_summary': [{'path': 'pkg.py', 'added': 1, 'removed': 1, 'is_test': False}],
        'src_diff': f'diff --git a/pkg.py b/pkg.py\n{_SECRET}\n',
        'test_diff': '',
        'acceptance_tests': '',
    }
    expected = {
        case.hunk_key(hunk_state): expected_hunk_questions('python', False),
        case.CHANGE_KEY: expected_change_questions(None),
    }
    meta = {
        'repo': 'x',
        'base': 'main',
        'head': 'deadbeef',
        'task_source': 'none',
        'model': 'jev-test',
        'date': '2026-01-01T00:00:00+00:00',
        'verdict': 'APPROVE',
        'score': 5,
    }

    case_dir = tmp_path / 'secret-case'
    case.write_case(case_dir, [hunk_state], change_state, expected, {'findings': [], 'counts': {}}, 'md', meta)

    hunk_text = (case_dir / 'state' / 'hunk-01.json').read_text()
    change_text = (case_dir / 'state' / 'change.json').read_text()
    assert _SECRET not in hunk_text
    assert _SECRET not in change_text
    assert '<redacted>' in hunk_text
    assert '<redacted>' in change_text
    # still valid JSON, structure intact.
    json.loads(hunk_text)
    json.loads(change_text)


# ---------------------------------------------------------------------------
# 3. `--case` combined with an explicit `--record` is an argparse error.
# ---------------------------------------------------------------------------


def test_case_with_record_is_an_argparse_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(['--case', str(tmp_path / 'case'), '--record', str(tmp_path / 'record')])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert '--case' in err
    assert '--record' in err
