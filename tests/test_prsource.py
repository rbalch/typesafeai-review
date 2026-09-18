"""Acceptance tests for RA-02: `--task` accepts a PR number or URL.

One test group per acceptance clause in `tasks/run-anywhere/RA-02-task-from-pr.md`:
the three PR-ref forms plus a path fall-through, `extract_brief` + `parse_task` on a
saved copy of PR #13's body, a missing `<details>` block raising `PRSourceError`, both
red-sha forms, and a `gh` failure surfacing as exit 1 from `cli.main` with the `gh`
stderr in the message. `gh` itself is never invoked -- `subprocess.run` is monkeypatched
in `typesafe_review.prsource`, so this suite never touches the network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from typesafe_review.prsource import (
    PRSourceError,
    PullRequest,
    extract_brief,
    extract_red_sha,
    fetch_pr,
    parse_pr_ref,
)
from typesafe_review.taskfile import Task, parse_task

FIXTURE_PR13 = Path(__file__).parent / 'fixtures' / 'pr_bodies' / 'pr13.md'


# ---------------------------------------------------------------------------
# parse_pr_ref: the three forms, plus a path falls through to None.
# ---------------------------------------------------------------------------


def test_parse_pr_ref_bare_number():
    assert parse_pr_ref('13') == 13


def test_parse_pr_ref_hash_number():
    assert parse_pr_ref('#13') == 13


def test_parse_pr_ref_github_url():
    assert parse_pr_ref('https://github.com/rbalch/typesafeai-review/pull/13') == 13


def test_parse_pr_ref_path_falls_through_to_none():
    assert parse_pr_ref('tasks/run-anywhere/RA-02-task-from-pr.md') is None
    assert parse_pr_ref('some/relative/path') is None


# ---------------------------------------------------------------------------
# extract_brief + parse_task on a saved copy of PR #13's body.
# ---------------------------------------------------------------------------


def test_extract_brief_then_parse_task_on_pr13_body_yields_t11_with_three_bullets():
    body = FIXTURE_PR13.read_text()
    brief = extract_brief(body)
    task = parse_task(brief, 'PR #13')
    assert isinstance(task, Task)
    assert task.id == 'T-11'
    assert len(task.acceptance) == 3


def test_extract_brief_with_no_details_block_raises():
    with pytest.raises(PRSourceError):
        extract_brief('Just a plain PR body with no task brief block.')


# ---------------------------------------------------------------------------
# extract_red_sha: both forms.
# ---------------------------------------------------------------------------


def test_extract_red_sha_structured_form():
    body = 'Some text\nRed: abc1234\nmore text'
    assert extract_red_sha(body) == 'abc1234'


def test_extract_red_sha_prose_form():
    body = FIXTURE_PR13.read_text()
    assert extract_red_sha(body) == 'c5a7548'


def test_extract_red_sha_missing_returns_none():
    assert extract_red_sha('nothing relevant here') is None


def test_extract_red_sha_structured_line_wins_over_prose_form_in_the_same_body():
    body = 'Evidence: red-then-green on 1111111\nmore text\nRed: 2222222\ntail'
    assert extract_red_sha(body) == '2222222'


# ---------------------------------------------------------------------------
# parse_pr_ref: edge cases.
# ---------------------------------------------------------------------------


def test_parse_pr_ref_bare_hash_with_no_digits_falls_through_to_none():
    assert parse_pr_ref('#') is None


def test_parse_pr_ref_zero_is_returned_literally_not_rejected():
    # `0` is syntactically all-digits; whether `0` is ever a real PR number is the
    # caller's problem (`fetch_pr`/`gh` will fail on it) -- `parse_pr_ref` only
    # recognises the *shape* of a ref, it never validates the number.
    assert parse_pr_ref('0') == 0


def test_parse_pr_ref_negative_number_falls_through_to_none():
    # `str.isdigit()` rejects a leading `-`, and it is not a GitHub PR URL either,
    # so `-1` is treated as a (nonsensical, but not this function's call) file path.
    assert parse_pr_ref('-1') is None


def test_parse_pr_ref_url_with_trailing_slash():
    assert parse_pr_ref('https://github.com/rbalch/typesafeai-review/pull/13/') == 13


def test_parse_pr_ref_url_ending_in_files():
    assert parse_pr_ref('https://github.com/rbalch/typesafeai-review/pull/13/files') == 13


def test_parse_pr_ref_task_file_path_falls_through_to_none():
    assert parse_pr_ref('tasks/13.md') is None


# ---------------------------------------------------------------------------
# fetch_pr: gh failure surfaces as PRSourceError with the gh stderr tail.
# ---------------------------------------------------------------------------


def test_fetch_pr_gh_failure_raises_prsourceerror_with_stderr(monkeypatch, tmp_path: Path):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == 'gh'
        return subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr='gh: no pull requests found\n')

    monkeypatch.setattr('typesafe_review.prsource.subprocess.run', fake_run)
    with pytest.raises(PRSourceError, match='no pull requests found'):
        fetch_pr(13, tmp_path)


def test_fetch_pr_gh_failure_stderr_is_redacted(monkeypatch, tmp_path: Path):
    """`checks.redact` (public, RA-02 fix round 1) scrubs anything shaped like
    `token=...` out of the `gh` stderr before it lands in `PRSourceError` -- a
    credential leaking into `ts-review`'s own error output would be exactly the
    kind of "path that reports success it did not verify" AGENTS.md rules out."""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, returncode=1, stdout='', stderr='gh: request failed: token=ghp_abc123 was rejected\n'
        )

    monkeypatch.setattr('typesafe_review.prsource.subprocess.run', fake_run)
    with pytest.raises(PRSourceError) as excinfo:
        fetch_pr(13, tmp_path)
    assert '<redacted>' in str(excinfo.value)
    assert 'ghp_abc123' not in str(excinfo.value)


def test_fetch_pr_gh_missing_raises_prsourceerror(monkeypatch, tmp_path: Path):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError('gh not found')

    monkeypatch.setattr('typesafe_review.prsource.subprocess.run', fake_run)
    with pytest.raises(PRSourceError):
        fetch_pr(13, tmp_path)


def test_fetch_pr_success_parses_json(monkeypatch, tmp_path: Path):
    payload = (
        '{"number": 13, "title": "Labelled fixtures", "body": "some body", '
        '"baseRefName": "develop", "headRefOid": "deadbeef", '
        '"mergeCommit": null, "state": "OPEN"}'
    )

    def fake_run(cmd, **kwargs):
        assert kwargs.get('cwd') == tmp_path
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=payload, stderr='')

    monkeypatch.setattr('typesafe_review.prsource.subprocess.run', fake_run)
    pr = fetch_pr(13, tmp_path)
    assert isinstance(pr, PullRequest)
    assert pr.number == 13
    assert pr.base_ref == 'develop'
    assert pr.head_sha == 'deadbeef'
    assert pr.merge_commit is None
    assert pr.state == 'OPEN'


# ---------------------------------------------------------------------------
# cli.main: gh failure -> exit 1, gh stderr in the message.
# ---------------------------------------------------------------------------


def test_cli_main_gh_failure_exits_1_with_gh_stderr(monkeypatch, tmp_path: Path, capsys):
    from typesafe_review.cli import main
    from typesafe_review.verdict import EXIT_TOOL_FAILURE

    repo = tmp_path / 'repo'
    repo.mkdir()
    git_env = {
        **os.environ,
        'GIT_AUTHOR_NAME': 'Test',
        'GIT_AUTHOR_EMAIL': 'test@example.com',
        'GIT_COMMITTER_NAME': 'Test',
        'GIT_COMMITTER_EMAIL': 'test@example.com',
    }
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '--allow-empty', '-m', 'init', '-q'], check=True, env=git_env)
    subprocess.run(['git', '-C', str(repo), 'branch', '-m', 'develop'], check=True)

    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == 'gh':
            return subprocess.CompletedProcess(cmd, returncode=1, stdout='', stderr='gh: authentication required\n')
        return real_run(cmd, **kwargs)

    monkeypatch.setattr('typesafe_review.prsource.subprocess.run', fake_run)

    rc = main(['--worktree', str(repo), '--task', '13'])
    assert rc == EXIT_TOOL_FAILURE
    captured = capsys.readouterr()
    assert 'authentication required' in captured.err
