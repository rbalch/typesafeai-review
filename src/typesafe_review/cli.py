"""`ts-review` entry point.

T-01 scope only: parse every flag from spec §3, resolve the base ref, delete stale
`review.md` / `review.json` in the worktree, and stop. No checks, no diff slicing, no
model call yet — those are later tasks, named in the "not implemented (T-NN)" messages
below so the CLI never claims work it has not done.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from typesafe_review.verdict import EXIT_TOOL_FAILURE

BASE_CANDIDATES = ('develop', 'main', 'master')

# Flags this task does not implement yet, and the task that will. Checked in this
# order, all before step 0 (stale-output cleanup) touches the worktree.
_NOT_IMPLEMENTED = (
    ('task', 'T-02'),
    ('red_sha', 'T-04'),
    ('dump_state', 'T-05'),
    ('record', 'T-07'),
    ('replay', 'T-07'),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='ts-review',
        description='AI-powered code review: deterministic checks + Jev per-hunk questions.',
    )
    parser.add_argument('--worktree', type=Path, default=None, help='path to the builder worktree to review')
    parser.add_argument('--task', type=Path, default=None, help='planner task file for this change')
    parser.add_argument('--red-sha', dest='red_sha', default=None, help='commit SHA of the failing acceptance tests')
    parser.add_argument(
        '--base',
        default=None,
        help='base ref to diff against (default: develop, falling back to main, master)',
    )
    parser.add_argument('--dump-state', type=Path, default=None, help='write state + questions as JSON and exit')
    parser.add_argument('--record', type=Path, default=None, help='record API responses to this directory')
    parser.add_argument('--replay', type=Path, default=None, help='replay recorded API responses from this directory')
    parser.add_argument('--calibrate', type=Path, default=None, help='run calibration against a labelled fixtures dir')
    return parser


def _worktree_root_error(path: Path) -> str | None:
    """Return an error message if `path` is not a git worktree root, else None."""
    if not path.is_dir():
        return f'not a git worktree: {path}'
    result = subprocess.run(
        ['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return f'not a git worktree: {path}'
    toplevel = Path(result.stdout.strip()).resolve()
    if toplevel != path.resolve():
        return f'not a worktree root: {path} (root is {toplevel})'
    return None


def _resolve_base(worktree: Path, base: str | None) -> str | None:
    candidates = (base,) if base else BASE_CANDIDATES
    for ref in candidates:
        result = subprocess.run(
            ['git', '-C', str(worktree), 'rev-parse', '--verify', ref],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return ref
    return None


def _clean_stale_outputs(worktree: Path) -> None:
    """Step 0: delete review.md / review.json at the worktree root, if present."""
    for name in ('review.md', 'review.json'):
        candidate = worktree / name
        if candidate.exists():
            candidate.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.calibrate is not None:
        print('not implemented (T-11)', file=sys.stderr)
        return EXIT_TOOL_FAILURE

    if args.worktree is None:
        parser.error('the following arguments are required: --worktree')

    for attr, task_id in _NOT_IMPLEMENTED:
        if getattr(args, attr) is not None:
            print(f'not implemented ({task_id})', file=sys.stderr)
            return EXIT_TOOL_FAILURE

    worktree: Path = args.worktree
    root_error = _worktree_root_error(worktree)
    if root_error is not None:
        print(root_error, file=sys.stderr)
        return EXIT_TOOL_FAILURE

    base = _resolve_base(worktree, args.base)
    if base is None:
        tried = args.base if args.base else ', '.join(BASE_CANDIDATES)
        print(f'no base ref found (tried: {tried})', file=sys.stderr)
        return EXIT_TOOL_FAILURE

    print(f'base: {base}', file=sys.stderr)

    _clean_stale_outputs(worktree)

    print('pipeline not implemented', file=sys.stderr)
    return EXIT_TOOL_FAILURE


if __name__ == '__main__':
    sys.exit(main())
