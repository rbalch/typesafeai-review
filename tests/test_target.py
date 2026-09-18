"""Acceptance tests for RA-03: review any ref (`--pr`/`--ref`/`--commit`/`--range`,
`--out`, default `--worktree`, and the temporary detached worktree ref modes run in).

One test (or a small group) per acceptance clause in
`tasks/run-anywhere/RA-03-review-any-ref.md`. Pure `resolve_target` tests build
throwaway repos the same way `tests/test_slicing.py` does; `--pr` tests stub
`typesafe_review.target.fetch_pr` so nothing touches the network or `gh`. The
`cli.main` level tests drive the real entry point, with `typesafe_review.pipeline
.ask_all` monkeypatched to a fake that never touches the network -- the same "stub
the network seam, run everything else for real" shape `_stub_gh` uses in
`tests/test_pipeline.py`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typesafe_sdk import SystemOneResponse, Usage

from typesafe_review import pipeline as pipeline_module
from typesafe_review.ask import AskResult, RequestItem
from typesafe_review.cli import main
from typesafe_review.prsource import PRSourceError, PullRequest
from typesafe_review.target import TargetError, resolve_target
from typesafe_review.verdict import EXIT_NEEDS_HUMAN, EXIT_TOOL_FAILURE

# --- shared throwaway-repo helpers (mirrors tests/test_slicing.py) -----------------


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout


def make_repo(tmp_path: Path, name: str = 'repo', branch: str = 'main') -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', branch)
    _git(repo, 'config', 'user.email', 't@example.com')
    _git(repo, 'config', 'user.name', 'Test')
    return repo


def write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def commit(repo: Path, message: str) -> str:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', message, '--allow-empty')
    return _git(repo, 'rev-parse', 'HEAD').strip()


def rev_parse(repo: Path, ref: str) -> str:
    return _git(repo, 'rev-parse', ref).strip()


def _args(**overrides: Any) -> argparse.Namespace:
    base_kwargs = {'range': None, 'commit': None, 'ref': None, 'pr': None}
    base_kwargs.update(overrides)
    return argparse.Namespace(**base_kwargs)


# --- (base_sha, head_sha) per mode --------------------------------------------------


def test_no_mode_flag_uses_merge_base_of_base_and_head(tmp_path: Path) -> None:
    """`base` (`develop`) moves past the fork point after the branch under review
    forked from it, so `develop`'s tip is a different commit from the merge-base --
    a plain `rev-parse(base)` (the bug fix round 1 found) would return the wrong,
    too-new `base_sha` here."""
    repo = make_repo(tmp_path, branch='develop')
    fork_sha = commit(repo, 'fork point')
    _git(repo, 'checkout', '-q', '-b', 'work')
    write(repo, 'a.txt', 'x\n')
    head_sha = commit(repo, 'change')
    _git(repo, 'checkout', '-q', 'develop')
    write(repo, 'unrelated.txt', 'y\n')
    commit(repo, 'develop moves on without this change')
    _git(repo, 'checkout', '-q', 'work')

    target = resolve_target(repo, _args(), base='develop')

    assert target.base_sha == fork_sha
    assert target.head_sha == head_sha
    assert target.source == 'worktree'


def test_range_two_dot(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    a_sha = commit(repo, 'a')
    write(repo, 'f.txt', 'y\n')
    b_sha = commit(repo, 'b')

    target = resolve_target(repo, _args(range=f'{a_sha}..{b_sha}'), base=None)

    assert target.base_sha == _git(repo, 'merge-base', a_sha, b_sha).strip()
    assert target.head_sha == b_sha
    assert target.source == 'range'


def test_range_three_dot(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    a_sha = commit(repo, 'a')
    _git(repo, 'checkout', '-q', '-b', 'side')
    write(repo, 'f.txt', 'y\n')
    b_sha = commit(repo, 'side change')

    target = resolve_target(repo, _args(range=f'{a_sha}...{b_sha}'), base=None)

    assert target.base_sha == _git(repo, 'merge-base', a_sha, b_sha).strip()
    assert target.head_sha == b_sha
    assert target.source == 'range'


def test_commit_mode_is_parent_and_self(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    commit(repo, 'base')
    write(repo, 'f.txt', 'y\n')
    s_sha = commit(repo, 'the commit')

    target = resolve_target(repo, _args(commit=s_sha), base=None)

    assert target.base_sha == rev_parse(repo, f'{s_sha}^')
    assert target.head_sha == s_sha
    assert target.source == 'commit'


def test_ref_mode_uses_merge_base_of_resolved_base_and_ref(tmp_path: Path) -> None:
    """`main` moves past the fork point after `feature` branches off it, so a plain
    `rev-parse('main')` (the bug fix round 1 found) would return the wrong, too-new
    `base_sha` here -- only `merge-base(main, feature)` gives the fork point."""
    repo = make_repo(tmp_path)
    fork_sha = commit(repo, 'fork point')
    _git(repo, 'checkout', '-q', '-b', 'feature')
    write(repo, 'f.txt', 'y\n')
    feature_sha = commit(repo, 'feature change')
    _git(repo, 'checkout', '-q', 'main')
    write(repo, 'unrelated.txt', 'y\n')
    commit(repo, 'main moves on without this change')

    target = resolve_target(repo, _args(ref='feature'), base='main')

    assert target.base_sha == fork_sha
    assert target.head_sha == feature_sha
    assert target.source == 'ref'


def test_unknown_ref_raises_target_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    commit(repo, 'base')

    with pytest.raises(TargetError):
        resolve_target(repo, _args(ref='no-such-ref'), base='main')


# --- --pr: open, squash-merged, two-parent-merged -----------------------------------


def _pull_request(**overrides: Any) -> PullRequest:
    defaults: dict[str, Any] = {
        'number': 13,
        'title': 'stub',
        'body': '',
        'base_ref': 'main',
        'head_sha': 'deadbeef',
        'merge_commit': None,
        'state': 'OPEN',
    }
    defaults.update(overrides)
    return PullRequest(**defaults)


def test_pr_mode_open_pr_diffs_base_ref_against_head_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    commit(repo, 'base')
    _git(repo, 'checkout', '-q', '-b', 'pr-branch')
    write(repo, 'f.txt', 'y\n')
    head_sha = commit(repo, 'pr change')
    _git(repo, 'checkout', '-q', 'main')

    monkeypatch.setattr(
        'typesafe_review.target.fetch_pr',
        lambda number, repo_dir: _pull_request(base_ref='main', head_sha=head_sha, merge_commit=None, state='OPEN'),
    )

    target = resolve_target(repo, _args(pr=13), base=None)

    assert target.base_sha == _git(repo, 'merge-base', 'main', head_sha).strip()
    assert target.head_sha == head_sha
    assert target.source == 'pr'


def test_pr_mode_squash_merged_uses_merge_commit_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    commit(repo, 'base')
    squash_sha = commit(repo, 'squashed PR change')  # one ordinary parent, not a merge commit

    monkeypatch.setattr(
        'typesafe_review.target.fetch_pr',
        lambda number, repo_dir: _pull_request(merge_commit=squash_sha, state='MERGED'),
    )

    target = resolve_target(repo, _args(pr=13), base=None)

    assert target.base_sha == rev_parse(repo, f'{squash_sha}^')
    assert target.head_sha == squash_sha
    assert target.source == 'pr'


def test_pr_mode_two_parent_merge_uses_merge_base_of_parents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` (`M^1`) moves past the fork point after `feature` branches off it and
    before the merge, so a plain `M^1` (the bug fix round 1 found) would return the
    wrong, too-new `base_sha` here -- only `merge-base(M^1, M^2)` gives the fork
    point `feature` actually forked from."""
    repo = make_repo(tmp_path)
    fork_sha = commit(repo, 'fork point')
    _git(repo, 'checkout', '-q', '-b', 'feature')
    write(repo, 'f.txt', 'y\n')
    b_sha = commit(repo, 'feature change')
    _git(repo, 'checkout', '-q', 'main')
    write(repo, 'unrelated.txt', 'y\n')
    commit(repo, 'main moves on before the merge')
    _git(repo, 'merge', '--no-ff', '-m', 'merge feature', 'feature')
    merge_sha = rev_parse(repo, 'HEAD')

    monkeypatch.setattr(
        'typesafe_review.target.fetch_pr',
        lambda number, repo_dir: _pull_request(merge_commit=merge_sha, state='MERGED'),
    )

    target = resolve_target(repo, _args(pr=13), base=None)

    assert target.base_sha == _git(repo, 'merge-base', f'{merge_sha}^1', f'{merge_sha}^2').strip()
    assert target.base_sha == fork_sha
    assert target.head_sha == b_sha
    assert target.source == 'pr'


def test_pr_mode_gh_failure_raises_target_error_not_pr_source_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`target.py` declares one error type; a `gh` failure (missing binary, not
    authenticated, PR not found) surfacing while resolving `--pr` must come out as
    `TargetError`, not the raw `PRSourceError` `fetch_pr` raises (fix round 1, item
    2)."""
    repo = make_repo(tmp_path)
    commit(repo, 'base')

    def _raise(number: int, repo_dir: Path) -> PullRequest:
        raise PRSourceError('gh pr view 13 failed: not authenticated')

    monkeypatch.setattr('typesafe_review.target.fetch_pr', _raise)

    with pytest.raises(TargetError) as exc:
        resolve_target(repo, _args(pr=13), base=None)
    assert 'not authenticated' in str(exc.value)


# --- --pr end to end: a `gh` failure never becomes a traceback, and costs exactly one
# `gh` call even though both target resolution and task resolution need the PR -------


def _gh_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    calls: list[list[str]],
    returncode: int = 0,
    body: str = '',
    head_sha: str = 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
) -> None:
    """Route only `gh pr view ...` to a fake response, recording every `gh`
    invocation in `calls`; every other `subprocess.run` call goes to the real
    `subprocess.run` -- the same shape `tests/test_pipeline.py`'s `_stub_gh` uses.
    Patches `typesafe_review.prsource.subprocess.run`, the one real seam both
    `target.py` and `cli.py`'s own `_resolve_task_and_red_sha` call through (both
    import the same `fetch_pr` from `prsource.py`), so it catches a double fetch
    regardless of which module made the second call. `head_sha` must be a real
    commit in the repo under test whenever the stub is used past the `gh` failure
    case below: `target.py` resolves it with real git calls, so a placeholder sha
    makes `resolve_target` fail before ever reaching `cli.py`'s own PR-reuse code.
    """
    real_run = subprocess.run
    payload = json.dumps(
        {
            'number': 13,
            'title': 'stub PR',
            'body': body,
            'baseRefName': 'main',
            'headRefOid': head_sha,
            'mergeCommit': None,
            'state': 'OPEN',
        }
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == 'gh':
            calls.append(cmd)
            if returncode != 0:
                return subprocess.CompletedProcess(
                    cmd, returncode=returncode, stdout='', stderr='gh: not authenticated'
                )
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=payload, stderr='')
        return real_run(cmd, **kwargs)

    monkeypatch.setattr('typesafe_review.prsource.subprocess.run', fake_run)


def test_pr_mode_gh_failure_exits_1_with_one_stderr_line_and_no_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    commit(repo, 'base')
    calls: list[list[str]] = []
    _gh_stub(monkeypatch, calls=calls, returncode=1)

    rc = main(['--worktree', str(repo), '--pr', '13'])
    err = capsys.readouterr().err

    assert rc == EXIT_TOOL_FAILURE
    non_empty_lines = [line for line in err.splitlines() if line]
    assert len(non_empty_lines) == 1
    assert 'gh pr view' in non_empty_lines[0]
    assert not (repo / 'ts-review.md').exists()
    assert not (repo / 'ts-review.json').exists()


_PR_TASK_TEXT = """\
---
id: T-PR
title: PR-sourced task
---

## Acceptance

- it works
"""


def _brief_body(task_text: str) -> str:
    return f'Some PR prose.\n\n<details><summary>Task brief</summary>\n\n{task_text}\n\n</details>\n'


def test_pr_mode_fetches_the_pr_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--pr 13` makes exactly one `gh pr view` call even though both target
    resolution (`resolve_target`) and task/red-sha resolution
    (`_resolve_task_and_red_sha`) need the PR (fix round 1, item 3)."""
    repo = make_repo(tmp_path)
    commit(repo, 'base')
    _git(repo, 'checkout', '-q', '-b', 'pr-branch')
    write(repo, 'f.txt', 'y\n')
    head_sha = commit(repo, 'pr change')
    _git(repo, 'checkout', '-q', 'main')

    calls: list[list[str]] = []
    _gh_stub(monkeypatch, calls=calls, body=_brief_body(_PR_TASK_TEXT), head_sha=head_sha)
    _stub_ask_all(monkeypatch)

    main(['--worktree', str(repo), '--pr', '13', '--replay', str(repo / '_unused')])

    gh_calls = [c for c in calls if c[:3] == ['gh', 'pr', 'view']]
    assert len(gh_calls) == 1


# --- cli.main: mode flags, default --worktree, temp worktree lifecycle, --out ------


async def _fake_ask_all(requests: list[RequestItem], *, model: str, recorder: object) -> AskResult:
    answers = {
        key: SystemOneResponse(model=model, usage=Usage(input_tokens=0, output_tokens=0), answers={})
        for key, _state, _questions in requests
    }
    return AskResult(answers=answers, requests=len(requests), input_tokens=0, model=model)


def _stub_ask_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('typesafe_review.pipeline.ask_all', _fake_ask_all)


def test_two_mode_flags_is_an_argparse_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(['--range', 'a..b', '--commit', 'x'])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert 'not allowed with' in err


def test_worktree_defaults_to_toplevel_of_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path)
    write(repo, 'pkg.py', 'def f():\n    return 1\n')
    commit(repo, 'base')
    write(repo, 'pkg.py', 'def f():\n    return 2\n')
    commit(repo, 'change')

    nested = repo / 'sub' / 'dir'
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    dump_dir = tmp_path / 'dump'
    rc = main(['--base', 'HEAD~1', '--dump-state', str(dump_dir)])

    assert rc == 0
    assert dump_dir.exists()


def _sample() -> Any:
    build_repo_path = Path(__file__).parent / 'fixtures' / 'repos' / 'sample' / 'build_repo.py'
    spec = importlib.util.spec_from_file_location('sample_build_repo_ra03', build_repo_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['sample_build_repo_ra03'] = module
    spec.loader.exec_module(module)
    return module


_build_repo = _sample()


@pytest.fixture()
def sample(tmp_path: Path):
    return _build_repo.build(tmp_path / 'sample')


def test_temp_worktree_exists_during_ref_mode_run_and_is_gone_after(sample, monkeypatch: pytest.MonkeyPatch) -> None:
    green_sha = rev_parse(sample.repo, 'HEAD')
    seen: dict[str, Any] = {}

    real_run = pipeline_module.run

    def spy_run(args, worktree, base, task, red_sha, task_source, red_sha_source, head, range_source, out_dir):
        seen['worktree'] = worktree
        seen['exists'] = worktree.is_dir()
        listing = subprocess.run(
            ['git', '-C', str(sample.repo), 'worktree', 'list'], capture_output=True, text=True, check=True
        ).stdout
        seen['listing_during_run'] = listing
        return real_run(args, worktree, base, task, red_sha, task_source, red_sha_source, head, range_source, out_dir)

    monkeypatch.setattr('typesafe_review.cli.pipeline.run', spy_run)
    _stub_ask_all(monkeypatch)

    rc = main(['--worktree', str(sample.repo), '--commit', green_sha, '--replay', str(sample.repo / '_unused')])

    assert rc == EXIT_NEEDS_HUMAN  # no --task
    assert seen['exists'] is True
    assert str(seen['worktree']) in seen['listing_during_run']
    assert seen['worktree'] != sample.repo

    listing_after = subprocess.run(
        ['git', '-C', str(sample.repo), 'worktree', 'list'], capture_output=True, text=True, check=True
    ).stdout
    assert str(seen['worktree']) not in listing_after
    assert not seen['worktree'].exists()


def test_temp_worktree_is_removed_even_when_the_pipeline_raises(sample, monkeypatch: pytest.MonkeyPatch) -> None:
    """`raising_run` raises `RuntimeError`, not one of `cli._PIPELINE_ERRORS` /
    `subprocess.SubprocessError` / `OSError` -- `_run_pipeline`'s own `except`
    clauses do not catch it, so it escapes all the way out of `main`. The `finally`
    around the temp worktree add/remove must still fire on that unwind (fix round 1,
    item 4: the previous version of this test raised `SlicingError`, which
    `_run_pipeline` *does* catch, so the assertions below passed whether or not the
    `finally` existed at all -- the mutation below proves that)."""
    green_sha = rev_parse(sample.repo, 'HEAD')
    captured: dict[str, Any] = {}

    def raising_run(args, worktree, base, task, red_sha, task_source, red_sha_source, head, range_source, out_dir):
        captured['worktree'] = worktree
        raise RuntimeError('boom')

    monkeypatch.setattr('typesafe_review.cli.pipeline.run', raising_run)

    with pytest.raises(RuntimeError, match='boom'):
        main(['--worktree', str(sample.repo), '--commit', green_sha, '--replay', str(sample.repo / '_unused')])

    listing_after = subprocess.run(
        ['git', '-C', str(sample.repo), 'worktree', 'list'], capture_output=True, text=True, check=True
    ).stdout
    assert str(captured['worktree']) not in listing_after
    assert not captured['worktree'].exists()


def test_ref_mode_run_never_touches_the_source_checkout(
    sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--commit`'s temporary detached worktree is where the pipeline runs and
    writes; the source repo's own `HEAD` and index are untouched. Outputs are routed
    to `--out` here so the only thing this test has to check is `HEAD`/the index --
    `test_out_flag_controls_where_outputs_land` covers where outputs land."""
    green_sha = rev_parse(sample.repo, 'HEAD')
    head_before = rev_parse(sample.repo, 'HEAD')
    staged_before = _git(sample.repo, 'diff', '--cached', '--name-only')
    tracked_diff_before = _git(sample.repo, 'diff', '--name-only')

    out_dir = tmp_path / 'out'
    out_dir.mkdir()
    _stub_ask_all(monkeypatch)
    main(
        [
            '--worktree',
            str(sample.repo),
            '--commit',
            green_sha,
            '--replay',
            str(sample.repo / '_unused'),
            '--out',
            str(out_dir),
        ]
    )

    assert rev_parse(sample.repo, 'HEAD') == head_before
    assert _git(sample.repo, 'diff', '--cached', '--name-only') == staged_before
    assert _git(sample.repo, 'diff', '--name-only') == tracked_diff_before


def test_out_flag_controls_where_outputs_land(sample, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    green_sha = rev_parse(sample.repo, 'HEAD')
    out_dir = tmp_path / 'out'
    out_dir.mkdir()

    _stub_ask_all(monkeypatch)
    rc = main(
        [
            '--worktree',
            str(sample.repo),
            '--commit',
            green_sha,
            '--replay',
            str(sample.repo / '_unused'),
            '--out',
            str(out_dir),
        ]
    )

    assert rc == EXIT_NEEDS_HUMAN
    assert (out_dir / 'ts-review.md').exists()
    assert (out_dir / 'ts-review.json').exists()
    assert not (sample.repo / 'ts-review.md').exists()
    assert not (sample.repo / 'ts-review.json').exists()


def test_commit_mode_replay_run_with_no_task_exits_3_needs_human_both_files_in_repo_root(
    sample, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact invocation from RA-03's acceptance: `--commit <sha> --replay <dir>`
    against a replayed sample repo -> exit 3, no `--task`, both output files present
    at the repo root (the default `--out` in a ref mode)."""
    green_sha = rev_parse(sample.repo, 'HEAD')
    _stub_ask_all(monkeypatch)

    rc = main(['--worktree', str(sample.repo), '--commit', green_sha, '--replay', str(sample.repo / '_unused')])

    assert rc == EXIT_NEEDS_HUMAN
    md_path = sample.repo / 'ts-review.md'
    json_path = sample.repo / 'ts-review.json'
    assert md_path.exists()
    assert json_path.exists()
    data = json.loads(json_path.read_text())
    assert data['stop_reason'] == 'missing_context'
    assert data['verdict'] == 'NEEDS_HUMAN'
    assert data['context']['task_source'] == 'none'
    assert data['head'] == green_sha
    assert data['range_source'] == 'commit'
