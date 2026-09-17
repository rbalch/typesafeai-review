"""Build the tiny target repo T-10's pipeline test and Manual QA record run use.

Not a checked-in `.git` -- a deterministic script that builds one in a directory the
caller supplies (a `tmp_path` in tests, or an explicit path for the one-off `--record`
run). Every file's content is a fixed literal string, so the diff text (and therefore
`ask.request_key`, which hashes state + questions + model) is stable across runs; only
the commit SHAs themselves differ run to run, and no state shape carries a SHA.

Shape: a `main` branch with one "base" commit (plays `develop`), and a `work` branch
that branches off it with a red commit (a failing acceptance test) followed by a green
commit (the fix, plus a swallowed exception and a public function missing type hints --
the two smells this fixture exists to prove the reviewer catches).

Run directly to build a copy for a live `--record` run:

    python tests/fixtures/repos/sample/build_repo.py /tmp/ts-review-sample

which prints a JSON line: `{"repo": ..., "task": ..., "red_sha": ..., "base": "main"}`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_PYPROJECT = """\
[project]
name = "fixture"
version = "0.0.0"
requires-python = ">=3.10"

[tool.uv]
package = false

[dependency-groups]
dev = ["pytest"]

[tool.pytest.ini_options]
pythonpath = ["."]
"""

_PKG_BASE = 'def thing():\n    return False\n'

_TEST_THING = 'from pkg import thing\n\n\ndef test_thing():\n    assert thing() is True\n'

# The green commit: fixes `thing()`, then adds two unrelated smells for the reviewer
# to catch -- `safe_thing` swallows an exception with no re-raise or stated reason,
# `compute` is a public function with no type hints at all.
_PKG_GREEN = """\
def thing():
    return True


def safe_thing():
    try:
        return thing()
    except Exception:
        return None


def compute(a, b):
    return a + b
"""

_TASK_MD = """\
---
id: T-SAMPLE
title: Fix thing() and report success safely
---

## Goal

Make `thing()` return `True`.

## Acceptance

- `thing()` returns `True`.
"""


@dataclass(frozen=True)
class SampleRepo:
    repo: Path
    task: Path
    red_sha: str
    base: str = 'main'


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, env=env)


def _rev_parse(repo: Path, ref: str) -> str:
    out = subprocess.run(['git', 'rev-parse', ref], cwd=repo, check=True, capture_output=True)
    return out.stdout.decode().strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', message)
    return _rev_parse(repo, 'HEAD')


def build(root: Path) -> SampleRepo:
    """Build the sample repo fresh under `root` (must not already exist)."""
    repo = root / 'repo'
    repo.mkdir(parents=True)

    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 'test@example.com')
    _git(repo, 'config', 'user.name', 'Test')

    (repo / 'pyproject.toml').write_text(_PYPROJECT)
    (repo / 'pkg.py').write_text(_PKG_BASE)
    _commit(repo, 'base: pkg.py with thing() unimplemented')

    _git(repo, 'checkout', '-q', '-b', 'work')

    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_thing.py').write_text(_TEST_THING)
    red_sha = _commit(repo, 'red: acceptance test for thing()')

    (repo / 'pkg.py').write_text(_PKG_GREEN)
    _commit(repo, 'green: fix thing(), add safe_thing() and compute()')

    task_path = root / 'task.md'
    task_path.write_text(_TASK_MD)

    return SampleRepo(repo=repo, task=task_path, red_sha=red_sha, base='main')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path, help='directory to build the repo under (must not exist)')
    args = parser.parse_args(argv)

    sample = build(args.root)
    print(
        json.dumps({'repo': str(sample.repo), 'task': str(sample.task), 'red_sha': sample.red_sha, 'base': sample.base})
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
