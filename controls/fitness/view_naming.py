#!/usr/bin/env python3
"""Fitness control: the generated view is RULES.md and is never named AGENTS.md.

governance: enforces DEC-0

Fails if any file under governance/views/ is named AGENTS.md, or if RULES.md is
absent. Two files sharing one name, one generated and one hand-written, with the
rule "agents read only the generated one" balanced on top, is a trap. See DEC-0.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VIEWS_DIR = REPO_ROOT / 'governance' / 'views'
FORBIDDEN_NAME = 'AGENTS.md'
REQUIRED_VIEW = 'RULES.md'


def main() -> int:
    violations: list[str] = []

    if not VIEWS_DIR.is_dir():
        print('FAIL [DEC-0] governance/views/ does not exist.', file=sys.stderr)
        print('    -> Run `make views` to generate it.', file=sys.stderr)
        return 1

    for path in sorted(VIEWS_DIR.rglob('*')):
        if path.is_file() and path.name == FORBIDDEN_NAME:
            violations.append(path.relative_to(REPO_ROOT).as_posix())

    for offender in violations:
        print(f'FAIL [DEC-0] {offender}', file=sys.stderr)
        print(
            f'    A generated view may not be named {FORBIDDEN_NAME}. That is the '
            f'hand-written agent contract at the repo root, and one name for both files '
            f'makes "agents read only the generated one" unenforceable.',
            file=sys.stderr,
        )
        print(f'    -> Rename it to {REQUIRED_VIEW} and update build_views.py.', file=sys.stderr)

    required = VIEWS_DIR / REQUIRED_VIEW
    if not required.is_file():
        violations.append(required.relative_to(REPO_ROOT).as_posix())
        print(f'FAIL [DEC-0] governance/views/{REQUIRED_VIEW} is missing.', file=sys.stderr)
        print(
            '    Agents have no rules to read. An absent view is indistinguishable from a repo with no rules at all.',
            file=sys.stderr,
        )
        print('    -> Run `make views`.', file=sys.stderr)

    if violations:
        return 1
    print(f'ok [DEC-0] governance/views/{REQUIRED_VIEW} present, no {FORBIDDEN_NAME} generated.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
