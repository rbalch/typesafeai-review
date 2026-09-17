#!/usr/bin/env python3
"""Fitness control: `git rev-parse --git-dir` never stands in as a repo-root guard.

governance: enforces DEC-1

`--git-dir` is true anywhere inside a git repository, including subdirectories and
worktrees whose root is elsewhere. Code that then treats the checked path as the
repository root (as `_worktree_root_error` in `cli.py` originally did — see DEC-1)
accepts a superset of the paths its own logic requires. `--show-toplevel` is the
narrower, honest check: it returns the actual root, so the caller can compare it to
the path it was given.

Scope: `src/` only. A file that calls `git rev-parse` with `--git-dir` among its
literal arguments, and never calls `git rev-parse` with `--show-toplevel` anywhere
in the same file, fails. This is a file-level pairing, not a per-call one — coarser
than proving the two calls guard the same code path, but `--git-dir` is a narrow,
deliberate flag with no other legitimate use in this codebase (every git call here
operates against a caller-supplied worktree path whose root matters), so its bare
presence is the smell, not just its use unpaired within one function.

This does not attempt the general claim behind ledger finding F-1 — "a guard whose
success branch is reached by a condition broader than the one the following code
relies on" — which also named `returncode != 0` from pytest and bare `except: return
default`. Neither of those is mechanically safe to enforce; see DEC-1's body.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIR = REPO_ROOT / 'src'
NARROW_FLAG = '--show-toplevel'
BROAD_FLAG = '--git-dir'


def _string_constants(node: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _git_dir_guard_lines(tree: ast.AST) -> list[int]:
    """Line numbers of list/tuple literals that call `git rev-parse --git-dir`."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            values = _string_constants(node)
            if 'rev-parse' in values and BROAD_FLAG in values:
                lines.append(node.lineno)
    return lines


def check_file(path: Path) -> list[tuple[Path, int]]:
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return []
    guard_lines = _git_dir_guard_lines(tree)
    if not guard_lines:
        return []
    if NARROW_FLAG in _string_constants(tree):
        return []
    return [(path, line) for line in guard_lines]


def main() -> int:
    if not SCAN_DIR.is_dir():
        print('ok [DEC-1] src/ absent, nothing to scan.')
        return 0

    violations: list[tuple[Path, int]] = []
    for path in sorted(SCAN_DIR.rglob('*.py')):
        violations.extend(check_file(path))

    if violations:
        for path, line in violations:
            rel = path.relative_to(REPO_ROOT).as_posix()
            print(f'FAIL [DEC-1] {rel}:{line}', file=sys.stderr)
            print(
                f'    `git rev-parse --git-dir` proves the path is inside a repo, not that '
                f'it is the root. Pair it with `git rev-parse {NARROW_FLAG}` and compare the '
                f'resolved path, the way `_worktree_root_error` does in cli.py, or drop '
                f'`{BROAD_FLAG}` and use `{NARROW_FLAG}` alone.',
                file=sys.stderr,
            )
            print('    -> see DEC-1', file=sys.stderr)
        return 1

    print('ok [DEC-1] no bare `rev-parse --git-dir` root guards under src/.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
