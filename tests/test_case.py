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

from typesafe_review import calibrate, case, pipeline
from typesafe_review.calibrate import run as run_calibrate
from typesafe_review.cli import main
from typesafe_review.questions import expected_change_questions, expected_hunk_questions
from typesafe_review.slicing import slice_diff
from typesafe_review.state import MAX_REQUEST_TOKENS, ChangeState, HunkState
from typesafe_review.verdict import EXIT_APPROVE, EXIT_CHANGES_REQUESTED, EXIT_NEEDS_HUMAN

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

    # responses/ was filled by mirroring the replay hits -- exactly these three
    # fixture files, pinned as literals (fix round 1 item 2): both sides of this
    # assertion must come from ground truth independent of the run's own output,
    # never from `keys.json`'s own `request_key`s (those are `ask.request_key`'s
    # output too, so comparing against them can never catch a recorder/key
    # mismatch -- it would always agree with itself). `_REPLAY_DIR` also carries a
    # fourth, older `00ecd8ee...` fixture kept only for a `test_pipeline.py` replay
    # that predates RA-06's diff-based `acceptance_tests`; this run never touches it.
    _SAMPLE_TASK_RESPONSE_NAMES = {
        '82a13ea6fa436ed6f43f890ac57cfaab37f819282dc0c621248c1e817ff87366.json',
        'd19bfe66f212c8aa42908e4b3b5cf9a34c76cd250eec7f7d659af26a09ff97c3.json',
        'c96e7160f3de90e6608541a7abec5fa9ee320bd1e70b83bee77c7a18257b6cd2.json',
    }
    responses_dir = case_dir / 'responses'
    assert {p.name for p in responses_dir.iterdir()} == _SAMPLE_TASK_RESPONSE_NAMES
    # Cross-check: `keys.json`'s own `request_key`s should agree with the pinned
    # set too, so a *disagreement* between them (not just a mismatch against reality)
    # also fails loudly here rather than surfacing as an unrelated-looking miss.
    assert {f'{info["request_key"]}.json' for info in keys.values()} == _SAMPLE_TASK_RESPONSE_NAMES

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


# ---------------------------------------------------------------------------
# 4. RA-05 scope item 6: `write_case` copies the task file to `<case>/task.md`
#    when a task was given, so `calibrate.load_case_task` can register the
#    case's `criterion_*` ids -- without it, a real case recorded with `--task`
#    crashed `--calibrate` with `KeyError: criterion_1_satisfied`.
# ---------------------------------------------------------------------------

_MINIMAL_TASK_MD = '---\nid: T-X\ntitle: X\n---\n\n## Acceptance\n\n- it works.\n'


def _minimal_case_args(tmp_path: Path) -> tuple[Path, list[HunkState], ChangeState, dict, dict]:
    hunk_state: HunkState = {
        'task': None,
        'file': {'path': 'pkg.py', 'language': 'python', 'is_test': False, 'is_new': False},
        'hunk': {'header': '@@ -1 +1 @@', 'diff': '-old\n+new\n', 'after': 'new\n'},
        'neighbours': {'same_module_helpers': [], 'tests_touching_file': []},
        'conventions': '',
    }
    change_state: ChangeState = {
        'task': None,
        'diff_summary': [{'path': 'pkg.py', 'added': 1, 'removed': 1, 'is_test': False}],
        'src_diff': 'diff --git a/pkg.py b/pkg.py\n',
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
    return tmp_path, [hunk_state], change_state, expected, meta


def test_write_case_with_task_text_writes_task_md_byte_equal_to_the_task_file(tmp_path: Path) -> None:
    task_path = tmp_path / 'task.md'
    task_path.write_text(_MINIMAL_TASK_MD)
    with task_path.open(newline='') as f:
        task_text = f.read()

    _, hunk_states, change_state, expected, meta = _minimal_case_args(tmp_path)
    case_dir = tmp_path / 'case-with-task'
    case.write_case(
        case_dir, hunk_states, change_state, expected, {'findings': [], 'counts': {}}, 'md', meta, task_text
    )

    written = case_dir / 'task.md'
    assert written.exists()
    assert written.read_bytes() == task_path.read_bytes()


def test_write_case_without_task_text_writes_no_task_md(tmp_path: Path) -> None:
    _, hunk_states, change_state, expected, meta = _minimal_case_args(tmp_path)
    case_dir = tmp_path / 'case-without-task'
    case.write_case(case_dir, hunk_states, change_state, expected, {'findings': [], 'counts': {}}, 'md', meta)

    assert not (case_dir / 'task.md').exists()


def test_calibrate_replays_a_real_case_recorded_with_task_and_criterion_ids(tmp_path: Path, sample) -> None:
    """End-to-end reproduction of the boundary reviewer's finding: a `--case` run
    with `--task` (so `keys.json`'s `<change>` entry carries `criterion_1_satisfied`
    / `criterion_1_tested`), followed by `--calibrate` on that case, must not crash."""
    case_dir = tmp_path / 'real-case'

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

    assert (case_dir / 'task.md').read_bytes() == sample.task.read_bytes()
    keys = json.loads((case_dir / 'state' / 'keys.json').read_text())
    assert 'criterion_1_satisfied' in keys[case.CHANGE_KEY]['questions']

    (case_dir / 'labels.json').write_text('{}')

    fixtures_dir = tmp_path / 'fixtures'
    fixtures_dir.mkdir()
    (case_dir).rename(fixtures_dir / 'real-case')

    calibrate_rc = calibrate.run(fixtures_dir)
    assert calibrate_rc == 0


def test_case_with_a_crlf_task_file_round_trips_task_md_byte_for_byte(sample, tmp_path: Path) -> None:
    """`cli._resolve_task_and_red_sha` reads the task file twice: once through
    `load_task` (universal newlines, for parsing), once through `open(newline='')`
    (exact bytes, for `task_text`). `load_task`'s own translation means a CRLF task
    file parses to the same `Task` as `sample.task`'s LF original -- same request
    key, so this still replays against `_REPLAY_DIR` -- but only the `newline=''`
    read preserves `\\r\\n` into `<case>/task.md`; a plain `open()`/`read_text()`
    there would silently translate it away and this test would catch that."""
    crlf_task = tmp_path / 'task-crlf.md'
    with crlf_task.open('w', newline='') as f:
        f.write(sample.task.read_text().replace('\n', '\r\n'))
    assert b'\r\n' in crlf_task.read_bytes()

    case_dir = tmp_path / 'case-crlf'
    rc = main(
        [
            '--worktree',
            str(sample.repo),
            '--task',
            str(crlf_task),
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
    assert (case_dir / 'task.md').read_bytes() == crlf_task.read_bytes()


# ---------------------------------------------------------------------------
# RA-06 fix round 1 item 5: a request over the token budget is never sent, so
# `--case` must omit it from `keys.json` (no `request_key`, no `responses/` file)
# and record it under `meta.json.skipped` instead -- a later `--calibrate` on that
# case must see a state that was never asked, not one silently missing an answer.
# ---------------------------------------------------------------------------


def test_skipped_hunk_is_omitted_from_keys_json_and_named_in_meta_skipped(
    sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build_hunk_states = pipeline.build_hunk_states

    def _padded_build_hunk_states(task, change, conventions):
        states = real_build_hunk_states(task, change, conventions)
        padded_first: HunkState = {
            **states[0],
            'hunk': {**states[0]['hunk'], 'diff': states[0]['hunk']['diff'] + ('x' * (MAX_REQUEST_TOKENS * 4 * 2))},
        }
        return [padded_first, *states[1:]]

    monkeypatch.setattr(pipeline, 'build_hunk_states', _padded_build_hunk_states)

    # RA-05 now writes `<case>/task.md` whenever a task was given, so this no
    # longer needs the no-task sidestep fix round 1 used here (that round's own
    # `calibrate.load_case_task` `KeyError` is exactly what RA-05 fixed) -- a
    # skipped hunk still forces `NEEDS_HUMAN` regardless of task presence
    # (`compose.py`'s `skipped_present` check), so the verdict assertion below is
    # unaffected by using `--task` + `_REPLAY_DIR` here now.
    case_dir = tmp_path / 'cases' / 'padded-hunk'
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
    assert rc == EXIT_NEEDS_HUMAN

    keys = json.loads((case_dir / 'state' / 'keys.json').read_text())
    # Sample has 2 hunks + `<change>` = 3 keys normally; the padded one is gone.
    assert len(keys) == 2
    assert case.CHANGE_KEY in keys
    for info in keys.values():
        assert 'request_key' in info

    meta = json.loads((case_dir / 'meta.json').read_text())
    assert len(meta['skipped']) == 1
    skipped_entry = meta['skipped'][0]
    assert skipped_entry['key'] == 'hunk-0'
    assert skipped_entry['budget'] == MAX_REQUEST_TOKENS
    assert skipped_entry['estimated_tokens'] > MAX_REQUEST_TOKENS

    # No `responses/` entry exists under the skipped key's would-be request_key --
    # there never was one to mirror.
    response_names = {p.stem for p in (case_dir / 'responses').iterdir()}
    request_keys = {info['request_key'] for info in keys.values()}
    assert response_names == request_keys

    (case_dir / 'labels.json').write_text('{}')
    calibrate_rc = run_calibrate(case_dir.parent)
    assert calibrate_rc == EXIT_APPROVE
