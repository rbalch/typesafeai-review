"""Resolve what to diff: `base_sha`, `head_sha`, from `--range`/`--commit`/`--ref`/
`--pr`, or today's default (merge-base(`<base>`, `HEAD`) vs `HEAD` of `--worktree`).

Scope item 1 of RA-03. Pure git subprocess calls plus `prsource.fetch_pr` for `--pr`;
no other I/O. `cli.py` resolves the mode from argparse, resolves `--base` for the two
modes that need it (`worktree`, `ref`), and calls `resolve_target` once with both.
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path

from typesafe_review.prsource import PRSourceError, PullRequest, fetch_pr


class TargetError(Exception):
    """A ref, commit, range or PR could not be resolved to a `(base_sha, head_sha)`
    pair: an unknown ref, a git subprocess failure, or a PR whose merge commit could
    not be inspected. Never swallowed -- an unresolved target must not fall back to
    reviewing the wrong range."""


@dataclass(frozen=True)
class Target:
    base_sha: str
    head_sha: str
    #: 'worktree' | 'range' | 'commit' | 'ref' | 'pr'
    source: str
    #: The fetched PR, for `source == 'pr'` only -- `None` otherwise. `cli.py` hands
    #: this straight to `_resolve_task_and_red_sha` so `--pr N` makes exactly one
    #: `gh` call (fix round 1, item 3), not one here and a second one resolving the
    #: task brief.
    pr: PullRequest | None = None


def _run(repo: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ['git', '-C', str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise TargetError(f'git {" ".join(args)} in {repo} failed to start: {e}') from e
    if result.returncode != 0:
        raise TargetError(f'git {" ".join(args)} in {repo} failed: {result.stderr.strip()}')
    return result.stdout.strip()


def _rev_parse(repo: Path, ref: str) -> str:
    """The full sha `ref` resolves to. Raises `TargetError` for an unknown ref."""
    return _run(repo, ['rev-parse', '--verify', ref])


def _merge_base(repo: Path, a: str, b: str) -> str:
    return _run(repo, ['merge-base', a, b])


def _parent_count(repo: Path, sha: str) -> int:
    line = _run(repo, ['rev-list', '--parents', '-n', '1', sha])
    return len(line.split()) - 1


def _split_range(range_str: str) -> tuple[str, str]:
    """`A..B` or `A...B` -> `(A, B)`. Three dots checked first: `A...B` also matches
    the two-dot pattern as a substring, so splitting on `..` first would leave a
    stray leading `.` on `B`."""
    if '...' in range_str:
        a, b = range_str.split('...', 1)
    elif '..' in range_str:
        a, b = range_str.split('..', 1)
    else:
        raise TargetError(f'--range {range_str!r} is not A..B or A...B')
    if not a or not b:
        raise TargetError(f'--range {range_str!r} is not A..B or A...B')
    return a, b


def _resolve_pr(repo: Path, pr_number: int) -> Target:
    try:
        pr = fetch_pr(pr_number, repo)
    except PRSourceError as e:
        # `target.py` declares one error type (`TargetError`); a `gh` failure --
        # missing binary, not authenticated, no GitHub remote, PR not found -- must
        # not escape as a raw `PRSourceError` past this module's own contract (fix
        # round 1, item 2).
        raise TargetError(str(e)) from e

    if pr.merge_commit is None:
        # Open PR: diff the PR's own base branch against its head, same as any other
        # in-flight branch.
        base_sha = _merge_base(repo, pr.base_ref, pr.head_sha)
        head_sha = _rev_parse(repo, pr.head_sha)
        return Target(base_sha=base_sha, head_sha=head_sha, source='pr', pr=pr)

    merge_sha = _rev_parse(repo, pr.merge_commit)
    if _parent_count(repo, merge_sha) <= 1:
        # Squash merge: one ordinary commit, no merge parents of its own.
        base_sha = _rev_parse(repo, f'{merge_sha}^')
        return Target(base_sha=base_sha, head_sha=merge_sha, source='pr', pr=pr)

    # A real two-parent merge commit: diff the branch's own history, not the merge
    # commit itself (which would also show the base branch moving under it).
    base_sha = _merge_base(repo, f'{merge_sha}^1', f'{merge_sha}^2')
    head_sha = _rev_parse(repo, f'{merge_sha}^2')
    return Target(base_sha=base_sha, head_sha=head_sha, source='pr', pr=pr)


def resolve_target(repo: Path, args: argparse.Namespace, base: str | None) -> Target:
    """`repo` is the source repo `--worktree` resolved to (explicit or defaulted to
    the cwd's toplevel). `base` is the base ref already resolved by `cli.py`
    (`_resolve_base`) -- only the `worktree` (no mode flag) and `ref` cases need it.

    Raises `TargetError` for an unknown ref/sha, a malformed `--range`, or a PR whose
    merge commit could not be inspected; never guesses past those.
    """
    if args.range is not None:
        a, b = _split_range(args.range)
        base_sha = _merge_base(repo, a, b)
        head_sha = _rev_parse(repo, b)
        return Target(base_sha=base_sha, head_sha=head_sha, source='range')

    if args.commit is not None:
        head_sha = _rev_parse(repo, args.commit)
        base_sha = _rev_parse(repo, f'{args.commit}^')
        return Target(base_sha=base_sha, head_sha=head_sha, source='commit')

    if args.ref is not None:
        if base is None:
            raise TargetError('no base ref to diff --ref against')
        base_sha = _merge_base(repo, base, args.ref)
        head_sha = _rev_parse(repo, args.ref)
        return Target(base_sha=base_sha, head_sha=head_sha, source='ref')

    if args.pr is not None:
        return _resolve_pr(repo, args.pr)

    if base is None:
        raise TargetError('no base ref to diff HEAD against')
    base_sha = _merge_base(repo, base, 'HEAD')
    head_sha = _rev_parse(repo, 'HEAD')
    return Target(base_sha=base_sha, head_sha=head_sha, source='worktree')
