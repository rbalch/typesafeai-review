"""Unit tests for `testrepo.py` (review fix round 1, item 4).

Covers the two-commit build directly (not just indirectly through
`calibrate.calibrate`), and the "`tree` not existing is treated as an empty tree"
branch `_sync_tree`'s docstring documents but no seed fixture case exercises: a
`before/`-only tree (a wholly new file) and an `after/`-only tree (a wholly deleted
file).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from typesafe_review.testrepo import build_two_stage_repo


def _diff_names(repo: Path, base_sha: str) -> list[str]:
    result = subprocess.run(
        ['git', '-C', str(repo), 'diff', '--name-status', f'{base_sha}..HEAD'],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def test_two_commit_build_diffs_before_to_after(tmp_path: Path) -> None:
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    before.mkdir()
    after.mkdir()
    (before / 'pkg.py').write_text('def thing():\n    return False\n')
    (after / 'pkg.py').write_text('def thing():\n    return True\n')

    built = build_two_stage_repo(tmp_path / 'work', before, after)

    assert built.repo.is_dir()
    assert (built.repo / 'pkg.py').read_text() == 'def thing():\n    return True\n'
    assert _diff_names(built.repo, built.base_sha) == ['M\tpkg.py']


def test_missing_before_directory_diffs_as_a_pure_add(tmp_path: Path) -> None:
    after = tmp_path / 'after'
    after.mkdir()
    (after / 'new_module.py').write_text('def brand_new():\n    return 1\n')

    # No `before/` at all: `_sync_tree` treats a missing tree as empty.
    built = build_two_stage_repo(tmp_path / 'work', tmp_path / 'before', after)

    assert _diff_names(built.repo, built.base_sha) == ['A\tnew_module.py']


def test_missing_after_directory_diffs_as_a_pure_delete(tmp_path: Path) -> None:
    before = tmp_path / 'before'
    before.mkdir()
    (before / 'old_module.py').write_text('def going_away():\n    return 1\n')

    # No `after/` at all: the file is removed between the two commits.
    built = build_two_stage_repo(tmp_path / 'work', before, tmp_path / 'after')

    assert _diff_names(built.repo, built.base_sha) == ['D\told_module.py']
