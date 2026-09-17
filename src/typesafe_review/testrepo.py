"""Build a throwaway two-commit git repo from a `before/`/`after/` directory pair.

Extracted for `calibrate.py` (T-11), which needs one such repo per fixture case, from
the shape `tests/fixtures/repos/sample/build_repo.py` (T-10) already established:
init a repo, commit one tree, replace it with a second tree, commit again, hand back
the repo path and the first commit's SHA as the diff base. `build_repo.py` keeps its
own literal-content generator as-is (its docstring explains why: stable diff text
across runs for one fixed sample) -- this module is the generic version, for building
a repo from an arbitrary directory tree on disk instead of literal strings.

`run_git`/`rev_parse` are public: `build_repo.py` imports them too (review fix round
1, item 4), so the two-line `subprocess.run(['git', ...])` wrapper exists in exactly
one place, not two independently-maintained copies.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class TestRepoError(Exception):
    """A git subprocess or filesystem operation needed to build a throwaway repo
    failed.

    Never swallowed: every subprocess call and directory sync this module makes is
    wrapped so no bare `OSError`/`subprocess.SubprocessError` escapes to the caller.
    """


@dataclass(frozen=True)
class BuiltRepo:
    repo: Path
    base_sha: str


def run_git(repo: Path, *args: str) -> None:
    """Run `git <args>` in `repo`, discarding output; raises `TestRepoError` naming
    the command on any failure (a non-zero exit, or the subprocess never starting)."""
    try:
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    except (subprocess.SubprocessError, OSError) as e:
        raise TestRepoError(f'git {" ".join(args)} in {repo} failed: {e}') from e


def rev_parse(repo: Path, ref: str) -> str:
    """`git rev-parse <ref>` in `repo`, stripped. Raises `TestRepoError` on failure."""
    try:
        result = subprocess.run(['git', 'rev-parse', ref], cwd=repo, check=True, capture_output=True)
    except (subprocess.SubprocessError, OSError) as e:
        raise TestRepoError(f'git rev-parse {ref} in {repo} failed: {e}') from e
    return result.stdout.decode().strip()


def _sync_tree(repo: Path, tree: Path) -> None:
    """Replace every tracked path under `repo` (everything but `.git`) with `tree`'s
    contents. `tree` not existing is treated as "empty tree" -- a case with no
    `before/` (a wholly new file) or no `after/` (a wholly deleted file) is valid."""
    try:
        for entry in repo.iterdir():
            if entry.name == '.git':
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        if tree.exists():
            for entry in tree.iterdir():
                dest = repo / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, dest)
                else:
                    shutil.copy2(entry, dest)
    except OSError as e:
        raise TestRepoError(f'failed to sync {tree} into {repo}: {e}') from e


def build_two_stage_repo(root: Path, before: Path, after: Path) -> BuiltRepo:
    """Build a throwaway repo under `root/repo`: commit `before`'s tree, then replace
    it with `after`'s tree and commit again.

    Returns the repo path and the first commit's SHA -- the base a caller diffs
    `HEAD` against to see exactly the `before` -> `after` change.
    """
    repo = root / 'repo'
    try:
        repo.mkdir(parents=True)
    except OSError as e:
        raise TestRepoError(f'cannot create {repo}: {e}') from e

    run_git(repo, 'init', '-q', '-b', 'main')
    run_git(repo, 'config', 'user.email', 'calibrate@example.com')
    run_git(repo, 'config', 'user.name', 'Calibrate')

    _sync_tree(repo, before)
    run_git(repo, 'add', '-A')
    run_git(repo, 'commit', '-q', '-m', 'before', '--allow-empty')
    base_sha = rev_parse(repo, 'HEAD')

    _sync_tree(repo, after)
    run_git(repo, 'add', '-A')
    run_git(repo, 'commit', '-q', '-m', 'after', '--allow-empty')

    return BuiltRepo(repo=repo, base_sha=base_sha)
