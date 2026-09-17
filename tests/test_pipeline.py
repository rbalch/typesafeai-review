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
import sys
from pathlib import Path

import pytest

from typesafe_review.cli import main
from typesafe_review.verdict import EXIT_CHANGES_REQUESTED, EXIT_NEEDS_HUMAN, EXIT_TOOL_FAILURE

_BUILD_REPO_PATH = Path(__file__).parent / 'fixtures' / 'repos' / 'sample' / 'build_repo.py'
_REPLAY_DIR = Path(__file__).parent / 'fixtures' / 'responses' / 'pipeline_sample'
_REPLAY_DIR_NO_TASK = Path(__file__).parent / 'fixtures' / 'responses' / 'pipeline_sample_no_task'


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
