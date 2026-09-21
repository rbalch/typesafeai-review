"""Acceptance tests for T-10: the pipeline wired end to end.

One test per acceptance clause in `tasks/typesafe-reviewer/T-10-pipeline-end-to-end.md`.
Every test drives `cli.main` -- the actual `uv run ts-review ...` entry point -- with
`--replay` against recorded responses for the sample repo built by
`tests/fixtures/repos/sample/build_repo.py`, so no test opens a network socket. The
replay fixtures under `tests/fixtures/responses/pipeline_sample/` are produced once by
a live `--record` run (see `docs/runs.md`); until they exist, the tests that need them
fail on the missing fixture (a `ReplayMiss` -> exit 1), not on an import error.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from typesafe_review import calibrate, pipeline
from typesafe_review.ask import Recorder, Replay
from typesafe_review.cli import main
from typesafe_review.questions import CHANGE_KEY, expected_change_questions
from typesafe_review.state import MAX_REQUEST_TOKENS, ChangeState, HunkState
from typesafe_review.taskfile import parse_task
from typesafe_review.verdict import EXIT_APPROVE, EXIT_CHANGES_REQUESTED, EXIT_NEEDS_HUMAN, EXIT_TOOL_FAILURE

_BUILD_REPO_PATH = Path(__file__).parent / 'fixtures' / 'repos' / 'sample' / 'build_repo.py'
_REPLAY_DIR = Path(__file__).parent / 'fixtures' / 'responses' / 'pipeline_sample'
_REPLAY_DIR_NO_TASK = Path(__file__).parent / 'fixtures' / 'responses' / 'pipeline_sample_no_task'

#: `parse_task` (RA-02 fix round 2) normalises outer blank lines before parsing, so a
#: PR-sourced `Task` for the sample repo's own task text hashes identically to
#: `load_task(sample.task)`'s -- the change-wide request for a PR-sourced task with
#: `sample.red_sha` replays straight out of `_REPLAY_DIR`, no copied fixture needed.
#: `red_sha=None` still moves the key, since `red_sha` feeds `acceptance_tests`
#: independently of the task text; this is that key, computed once offline via
#: `ask.request_key` against `build_change_state(parse_task(...), change,
#: load_acceptance_tests(repo, None))` -- never over the network.
_CHANGE_KEY_PR_TASK_NO_RED_SHA = '08398ddf8c2e5a581402f81eecb2f924f5791b66744dd66f7a68092e27147485'


def _load_build_repo():
    spec = importlib.util.spec_from_file_location('sample_build_repo', _BUILD_REPO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['sample_build_repo'] = module
    spec.loader.exec_module(module)
    return module


_build_repo = _load_build_repo()


@pytest.fixture()
def sample(tmp_path: Path):
    return _build_repo.build(tmp_path / 'sample')


def _read_outputs(worktree: Path) -> tuple[str, dict]:
    md = (worktree / 'ts-review.md').read_text()
    data = json.loads((worktree / 'ts-review.json').read_text())
    return md, data


def _stub_gh(monkeypatch: pytest.MonkeyPatch, body: str, *, pr_number: int = 13) -> None:
    """Route only `gh pr view <pr_number> ...` to a fake successful response
    carrying `body`; every other `subprocess.run` call (the git calls `cli.main`
    itself makes) goes to the real `subprocess.run` (RA-02 fix round 1)."""
    real_run = subprocess.run
    payload = json.dumps(
        {
            'number': pr_number,
            'title': 'stub PR',
            'body': body,
            'baseRefName': 'main',
            'headRefOid': 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
            'mergeCommit': None,
            'state': 'OPEN',
        }
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == 'gh':
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=payload, stderr='')
        return real_run(cmd, **kwargs)

    monkeypatch.setattr('typesafe_review.prsource.subprocess.run', fake_run)


def _brief_body(task_text: str, *, red_sha: str | None = None) -> str:
    """A PR body shaped like `orchestrate`'s own: a `Task brief` block, optionally
    followed by a `Red:` line -- the same shape `prsource.extract_brief` /
    `extract_red_sha` parse."""
    red_line = f'\nRed: {red_sha}\n' if red_sha else ''
    return f'Some PR prose.{red_line}\n<details><summary>Task brief</summary>\n\n{task_text}\n\n</details>\n'


def test_task_from_pr_number_resolves_context_pr_and_carries_the_brief_through(
    sample, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--task 13` with `gh` stubbed to return a PR body wrapping the sample repo's
    own task file: `ts-review.json`'s `context.task_source == "pr"`, and the parsed
    brief's acceptance criterion made it into the change-wide question set. Replays
    straight from `_REPLAY_DIR` -- no copied fixture, no offline key computation --
    because `parse_task`'s outer-blank-line normalisation (RA-02 fix round 2) makes
    a PR-sourced `Task` for this text hash identically to `load_task(sample.task)`'s,
    exactly like `test_full_replay_run_exits_2_and_flags_both_smells` below. Mutation:
    hardcoding `task_source='file'` in `cli.py` makes the `context` assertion below
    fail; undoing the normalisation makes the run miss the replay fixture entirely
    (exit 1), not just the `context` assertion.
    """
    task_text = sample.task.read_text()
    body = _brief_body(task_text)
    _stub_gh(monkeypatch, body)

    rc = main(
        [
            '--worktree',
            str(sample.repo),
            '--task',
            '13',
            '--red-sha',
            sample.red_sha,
            '--base',
            sample.base,
            '--replay',
            str(_REPLAY_DIR),
        ]
    )
    assert rc == EXIT_CHANGES_REQUESTED

    _, data = _read_outputs(sample.repo)
    assert data['context'] == {'task_source': 'pr', 'red_sha_source': 'flag'}

    # The brief the PR carried really is the one that reached `expected_change_questions`.
    task = parse_task(task_text, 'PR #13')
    assert 'criterion_1_satisfied' in expected_change_questions(task)


def test_pr_red_sha_that_does_not_exist_is_dropped_with_a_stderr_note(
    sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `Red:` sha in the PR body that `git cat-file -e <sha>^{commit}` cannot
    confirm in the worktree is never used: `red_sha_source` is `"none"`, not `"pr"`,
    and a stderr line says why. Mutation: deleting the `git cat-file` guard in
    `cli._resolve_task_and_red_sha` makes this fail (the fabricated sha reaches
    `run_checks`/`load_acceptance_tests`, which then fail on a real git error
    instead of writing `ts-review.json` with `red_sha_source: "none"`)."""
    task_text = sample.task.read_text()
    missing_sha = 'deadbee0'
    body = _brief_body(task_text, red_sha=missing_sha)
    _stub_gh(monkeypatch, body)

    # `red_sha=None` empties `acceptance_tests`, so the change-wide key moves off
    # `_REPLAY_DIR`'s even though the (now-normalised) task text is identical; the
    # two hunk keys don't move (a hunk's own `task` sub-state never carries
    # `criteria_text`), so only the change-wide fixture needs copying under its own
    # key -- content doesn't need to answer the exact question set sent (`compose`
    # treats a missing id as unanswered, not fatal).
    replay_dir = tmp_path / 'replay-red-sha-none'
    replay_dir.mkdir()
    shutil.copy(_REPLAY_DIR / '82a13ea6fa436ed6f43f890ac57cfaab37f819282dc0c621248c1e817ff87366.json', replay_dir)
    shutil.copy(_REPLAY_DIR / 'd19bfe66f212c8aa42908e4b3b5cf9a34c76cd250eec7f7d659af26a09ff97c3.json', replay_dir)
    shutil.copy(
        _REPLAY_DIR / '00ecd8ee972cb28ac6f25d0915726b5858258ef48415b075016a3d779d74b41c.json',
        replay_dir / f'{_CHANGE_KEY_PR_TASK_NO_RED_SHA}.json',
    )

    rc = main(
        [
            '--worktree',
            str(sample.repo),
            '--task',
            '13',
            '--base',
            sample.base,
            '--replay',
            str(replay_dir),
        ]
    )
    assert rc != EXIT_TOOL_FAILURE

    captured = capsys.readouterr()
    assert missing_sha in captured.err
    assert 'not found' in captured.err

    _, data = _read_outputs(sample.repo)
    assert data['context'] == {'task_source': 'pr', 'red_sha_source': 'none'}


def test_full_replay_run_exits_2_and_flags_both_smells(sample) -> None:
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
        ]
    )
    assert rc == EXIT_CHANGES_REQUESTED

    md, data = _read_outputs(sample.repo)
    finding_ids = {f['question_id'] for f in data['findings']}
    assert 'swallows_exception' in finding_ids
    assert 'missing_type_hints_public' in finding_ids
    # `ts-review.md` never carries a raw question id (render.py's issue/why/fix
    # templates, not the id itself) -- its own findings text is the md-side proof.
    assert 'Swallowing an exception' in md or 'swallows_exception' in md
    assert 'no type hints' in md
    assert data['verdict'] == 'CHANGES_REQUESTED'
    # RA-02: `--task <path>` and `--red-sha <sha>` are both flags/files, not a PR.
    assert data['context'] == {'task_source': 'file', 'red_sha_source': 'flag'}


def test_replay_run_without_task_exits_3_missing_context(sample) -> None:
    rc = main(
        [
            '--worktree',
            str(sample.repo),
            '--red-sha',
            sample.red_sha,
            '--base',
            sample.base,
            '--replay',
            str(_REPLAY_DIR_NO_TASK),
        ]
    )
    assert rc == EXIT_NEEDS_HUMAN

    _, data = _read_outputs(sample.repo)
    assert data['stop_reason'] == 'missing_context'
    assert data['verdict'] == 'NEEDS_HUMAN'


def test_replay_miss_exits_1_and_leaves_no_outputs(sample, tmp_path: Path) -> None:
    empty_replay = tmp_path / 'no-such-replay-dir'
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
            str(empty_replay),
        ]
    )
    assert rc == EXIT_TOOL_FAILURE
    assert not (sample.repo / 'ts-review.md').exists()
    assert not (sample.repo / 'ts-review.json').exists()


# ---------------------------------------------------------------------------
# RA-06: a request whose estimate exceeds `MAX_REQUEST_TOKENS` never reaches the
# recorder at all -- it is composed as `NEEDS_HUMAN` / `state_too_large` in code,
# never sent and never a 400.
# ---------------------------------------------------------------------------


def _make_hunk_state(diff_padding: int = 0) -> HunkState:
    diff = '@@ -1,1 +1,1 @@ def f\n-old\n+new\n'
    if diff_padding:
        diff = diff + ('x' * diff_padding)
    return {
        'task': None,
        'file': {'path': 'src/pkg/mod.py', 'language': 'python', 'is_test': False, 'is_new': False},
        'hunk': {'header': '@@ -1,1 +1,1 @@ def f', 'diff': diff, 'after': 'def f():\n    return new\n'},
        'neighbours': {'same_module_helpers': [], 'tests_touching_file': []},
        'conventions': '',
    }


def _make_change_state(src_diff_padding: int = 0) -> ChangeState:
    src_diff = 'small src diff'
    if src_diff_padding:
        src_diff = src_diff + ('x' * src_diff_padding)
    return {
        'task': None,
        'diff_summary': [],
        'src_diff': src_diff,
        'test_diff': '',
        'acceptance_tests': '',
    }


def test_build_requests_skips_a_hunk_state_over_the_token_budget() -> None:
    small_hunk = _make_hunk_state()
    # `MAX_REQUEST_TOKENS * 4` bytes of padding alone estimates to roughly
    # `MAX_REQUEST_TOKENS` tokens; doubling it clears the budget with margin.
    huge_hunk = _make_hunk_state(diff_padding=MAX_REQUEST_TOKENS * 4 * 2)
    change_state = _make_change_state()

    requests, skipped = pipeline._build_requests(None, [small_hunk, huge_hunk], change_state)

    assert {key for key, _, _ in requests} == {'hunk-0', 'change'}
    assert len(skipped) == 1
    skipped_key, estimate = skipped[0]
    assert skipped_key == 'hunk-1'
    assert estimate > MAX_REQUEST_TOKENS


def test_build_requests_skips_a_change_state_over_the_token_budget_hunks_unaffected() -> None:
    small_hunk = _make_hunk_state()
    huge_change = _make_change_state(src_diff_padding=MAX_REQUEST_TOKENS * 4 * 2)

    requests, skipped = pipeline._build_requests(None, [small_hunk], huge_change)

    assert {key for key, _, _ in requests} == {'hunk-0'}
    assert len(skipped) == 1
    skipped_key, estimate = skipped[0]
    assert skipped_key == 'change'
    assert estimate > MAX_REQUEST_TOKENS


class _CountingRecorder:
    """Wraps a real `Recorder`, remembering every `hunk_key` `ask_all` asked it to
    load -- a skipped request must never show up here at all, since it never
    reaches `ask_all`."""

    def __init__(self, inner: Recorder) -> None:
        self._inner = inner
        self.hunk_keys_seen: list[str] = []

    def load(self, key: str, hunk_key: str) -> bytes | None:
        self.hunk_keys_seen.append(hunk_key)
        return self._inner.load(key, hunk_key)

    def save(self, key: str, body: bytes) -> None:
        self._inner.save(key, body)


def test_hunk_over_budget_is_never_sent_and_run_reports_state_too_large(
    sample, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build_hunk_states = pipeline.build_hunk_states

    def _padded_build_hunk_states(task, change, conventions):
        states = real_build_hunk_states(task, change, conventions)
        # Pad the first hunk's diff well past `MAX_REQUEST_TOKENS` while staying
        # far below `MAX_HUNK_STATE_BYTES` (1 MiB) -- exactly the "below the hard
        # cap, over the soft budget" case this task exists for.
        padded_first: HunkState = {
            **states[0],
            'hunk': {**states[0]['hunk'], 'diff': states[0]['hunk']['diff'] + ('x' * 40000)},
        }
        return [padded_first, *states[1:]]

    monkeypatch.setattr(pipeline, 'build_hunk_states', _padded_build_hunk_states)

    counting = _CountingRecorder(Replay(_REPLAY_DIR))
    monkeypatch.setattr(pipeline, 'resolve_recorder', lambda args: counting)

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
        ]
    )

    assert rc == EXIT_NEEDS_HUMAN
    # The counting fake proves the padded hunk's request was never handed to the
    # recorder -- `_build_requests` filtered it out before `ask_all` ran.
    assert 'hunk-0' not in counting.hunk_keys_seen

    md, data = _read_outputs(sample.repo)
    assert data['verdict'] == 'NEEDS_HUMAN'
    assert data['stop_reason'] == 'state_too_large'
    assert len(data['skipped']) == 1
    skipped_entry = data['skipped'][0]
    assert skipped_entry['key'] == 'hunk-0'
    assert skipped_entry['budget'] == MAX_REQUEST_TOKENS
    assert skipped_entry['estimated_tokens'] > MAX_REQUEST_TOKENS
    assert 'hunk-0' in md
    assert str(MAX_REQUEST_TOKENS) in md


def test_change_state_over_budget_is_never_sent_while_hunk_requests_still_go_out(
    sample, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build_change_state = pipeline.build_change_state

    def _padded_build_change_state(task, change, acceptance_tests):
        state = real_build_change_state(task, change, acceptance_tests)
        return {**state, 'src_diff': state['src_diff'] + ('x' * 40000)}

    monkeypatch.setattr(pipeline, 'build_change_state', _padded_build_change_state)

    counting = _CountingRecorder(Replay(_REPLAY_DIR))
    monkeypatch.setattr(pipeline, 'resolve_recorder', lambda args: counting)

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
        ]
    )

    assert rc == EXIT_NEEDS_HUMAN
    assert 'change' not in counting.hunk_keys_seen
    # Hunk requests were unaffected: both still went to the recorder.
    assert 'hunk-0' in counting.hunk_keys_seen
    assert 'hunk-1' in counting.hunk_keys_seen

    _, data = _read_outputs(sample.repo)
    assert data['stop_reason'] == 'state_too_large'
    assert len(data['skipped']) == 1
    skipped_entry = data['skipped'][0]
    assert skipped_entry['key'] == 'change'
    assert skipped_entry['budget'] == MAX_REQUEST_TOKENS
    assert skipped_entry['estimated_tokens'] > MAX_REQUEST_TOKENS
    # The hunk findings this fixture always fires still made it through untouched.
    finding_ids = {f['question_id'] for f in data['findings']}
    assert 'swallows_exception' in finding_ids
    assert 'missing_type_hints_public' in finding_ids


def test_stale_outputs_removed_before_a_failing_run(sample, tmp_path: Path) -> None:
    (sample.repo / 'ts-review.md').write_text('stale markdown from a previous run\n')
    (sample.repo / 'ts-review.json').write_text('{"stale": true}')

    empty_replay = tmp_path / 'no-such-replay-dir-either'
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
            str(empty_replay),
        ]
    )
    assert rc == EXIT_TOOL_FAILURE
    assert not (sample.repo / 'ts-review.md').exists()
    assert not (sample.repo / 'ts-review.json').exists()


# ---------------------------------------------------------------------------
# RA-05 fix round 2: a PR-sourced `--task` still has no file on disk, so
# `write_case` must copy the *extracted brief text* (not a file path) to
# `<case>/task.md` -- without it, `keys.json`'s `<change>` entry carries
# `criterion_1_satisfied`/`criterion_1_tested` (the PR brief has one acceptance
# bullet, same as the sample repo's own task.md) and `--calibrate` on the
# resulting case crashed with `KeyError: 'criterion_1_satisfied'`, exactly as it
# did for a file-sourced `--task` before fix round 1.
# ---------------------------------------------------------------------------

#: `extract_brief` on `_brief_body(sample.task.read_text())`, pinned as a literal
#: (not computed via `extract_brief` here) so this test does not validate the
#: production code against itself.
_EXPECTED_PR_BRIEF_TEXT = (
    '---\n'
    'id: T-SAMPLE\n'
    'title: Fix thing() and report success safely\n'
    '---\n'
    '\n'
    '## Goal\n'
    '\n'
    'Make `thing()` return `True`.\n'
    '\n'
    '## Acceptance\n'
    '\n'
    '- `thing()` returns `True`.'
)


def test_pr_sourced_case_writes_task_md_from_the_brief_and_calibrate_accepts_it(
    sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_text = sample.task.read_text()
    body = _brief_body(task_text)
    _stub_gh(monkeypatch, body)

    case_dir = tmp_path / 'pr-case'
    rc = main(
        [
            '--worktree',
            str(sample.repo),
            '--task',
            '13',
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

    assert (case_dir / 'task.md').read_text() == _EXPECTED_PR_BRIEF_TEXT

    keys = json.loads((case_dir / 'state' / 'keys.json').read_text())
    assert 'criterion_1_satisfied' in keys[CHANGE_KEY]['questions']

    (case_dir / 'labels.json').write_text('{}')
    fixtures_dir = tmp_path / 'fixtures'
    fixtures_dir.mkdir()
    case_dir.rename(fixtures_dir / 'pr-case')

    assert calibrate.run(fixtures_dir) == EXIT_APPROVE
