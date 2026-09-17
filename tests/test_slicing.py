"""Acceptance tests for `typesafe_review.slicing` (T-03).

One test per acceptance clause in tasks/typesafe-reviewer/T-03-slicing.md. Every repo
is a throwaway git repo built with `git` subprocess calls in a tmp_path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from typesafe_review.slicing import MAX_HUNK_LINES, MAX_SRC_DIFF_BYTES, SlicingError, slice_diff


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ['git', *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.email', 't@example.com')
    _git(repo, 'config', 'user.name', 'Test')
    return repo


def write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def commit(repo: Path, message: str) -> None:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', message)


def tag_base(repo: Path) -> None:
    _git(repo, 'tag', 'base')


def test_two_hunks_in_one_file_yield_two_hunks_with_correct_headers(tmp_path):
    repo = make_repo(tmp_path)
    lines = [f'line{i}\n' for i in range(30)]
    write(repo, 'mod.py', ''.join(lines))
    commit(repo, 'base')
    tag_base(repo)

    lines[2] = 'changed-top\n'
    lines[25] = 'changed-bottom\n'
    write(repo, 'mod.py', ''.join(lines))
    commit(repo, 'two edits')

    change = slice_diff(repo, 'base')
    file_hunks = [h for h in change.hunks if h.path == 'mod.py']
    assert len(file_hunks) == 2
    for h in file_hunks:
        assert h.header.startswith('@@')


def test_method_edit_symbol_equals_method_name(tmp_path):
    repo = make_repo(tmp_path)
    write(
        repo,
        'pkg.py',
        'class Widget:\n    def render(self):\n        return 1\n',
    )
    commit(repo, 'base')
    tag_base(repo)

    write(
        repo,
        'pkg.py',
        'class Widget:\n    def render(self):\n        return 2\n',
    )
    commit(repo, 'edit method')

    change = slice_diff(repo, 'base')
    assert len(change.hunks) == 1
    assert change.hunks[0].symbol == 'render'


def test_after_window_at_file_start(tmp_path):
    repo = make_repo(tmp_path)
    lines = [f'line{i}\n' for i in range(60)]
    write(repo, 'mod.py', ''.join(lines))
    commit(repo, 'base')
    tag_base(repo)

    lines[0] = 'changed0\n'
    write(repo, 'mod.py', ''.join(lines))
    commit(repo, 'edit start')

    change = slice_diff(repo, 'base')
    assert len(change.hunks) == 1
    after = change.hunks[0].after
    # Window is clamped at the top of the file: line0 (index 0) through
    # line0 + 20 lines of context.
    assert 'changed0' in after
    assert 'line20\n' in after
    assert 'line21\n' not in after


def test_after_window_at_file_end(tmp_path):
    repo = make_repo(tmp_path)
    lines = [f'line{i}\n' for i in range(60)]
    write(repo, 'mod.py', ''.join(lines))
    commit(repo, 'base')
    tag_base(repo)

    lines[59] = 'changed59\n'
    write(repo, 'mod.py', ''.join(lines))
    commit(repo, 'edit end')

    change = slice_diff(repo, 'base')
    assert len(change.hunks) == 1
    after = change.hunks[0].after
    assert 'changed59' in after
    assert 'line39\n' in after
    assert 'line38\n' not in after


def test_401_line_hunk_is_truncated_and_flagged(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'big.py', 'start\n')
    commit(repo, 'base')
    tag_base(repo)

    added = ''.join(f'added{i}\n' for i in range(401))
    write(repo, 'big.py', 'start\n' + added)
    commit(repo, 'add 401 lines')

    change = slice_diff(repo, 'base')
    assert len(change.hunks) == 1
    hunk = change.hunks[0]
    assert hunk.truncated is True
    body_lines = hunk.diff.splitlines()[1:]
    assert len(body_lines) <= MAX_HUNK_LINES


def test_is_test_for_three_patterns(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'src/pkg.py', 'x = 1\n')
    write(repo, 'tests/whatever.py', 'x = 1\n')
    write(repo, 'test_foo.py', 'x = 1\n')
    write(repo, 'bar_test.py', 'x = 1\n')
    commit(repo, 'base')
    tag_base(repo)

    write(repo, 'src/pkg.py', 'x = 2\n')
    write(repo, 'tests/whatever.py', 'x = 2\n')
    write(repo, 'test_foo.py', 'x = 2\n')
    write(repo, 'bar_test.py', 'x = 2\n')
    commit(repo, 'edit all')

    change = slice_diff(repo, 'base')
    by_path = {h.path: h for h in change.hunks}
    assert by_path['src/pkg.py'].is_test is False
    assert by_path['tests/whatever.py'].is_test is True
    assert by_path['test_foo.py'].is_test is True
    assert by_path['bar_test.py'].is_test is True


def test_helper_signatures_include_return_annotation_and_private_function(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'mod.py', 'x = 1\n')
    commit(repo, 'base')
    tag_base(repo)

    write(
        repo,
        'mod.py',
        'def public(a: int) -> str:\n    return str(a)\n\n\ndef _private(b):\n    return b\n',
    )
    commit(repo, 'add functions')

    change = slice_diff(repo, 'base')
    hunk = change.hunks[0]
    sigs = hunk.neighbours.same_module_helpers
    assert any('public' in s and '->' in s and 'str' in s for s in sigs)
    assert any('_private' in s for s in sigs)


def test_test_file_importing_module_appears_in_tests_touching_file(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'pkg/mod.py', 'x = 1\n')
    write(repo, 'tests/test_mod.py', 'from pkg.mod import x\n')
    commit(repo, 'base')
    tag_base(repo)

    write(repo, 'pkg/mod.py', 'x = 2\n')
    commit(repo, 'edit mod')

    change = slice_diff(repo, 'base')
    hunk = next(h for h in change.hunks if h.path == 'pkg/mod.py')
    assert 'tests/test_mod.py' in hunk.neighbours.tests_touching_file


def test_src_and_test_file_change_partitions_diffs(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'src/pkg.py', 'x = 1\n')
    write(repo, 'tests/test_pkg.py', 'x = 1\n')
    commit(repo, 'base')
    tag_base(repo)

    write(repo, 'src/pkg.py', 'x = 2\n')
    write(repo, 'tests/test_pkg.py', 'x = 2\n')
    commit(repo, 'edit both')

    change = slice_diff(repo, 'base')
    assert 'src/pkg.py' in change.src_diff
    assert 'tests/test_pkg.py' not in change.src_diff
    assert 'tests/test_pkg.py' in change.test_diff
    assert 'src/pkg.py' not in change.test_diff


def test_src_diff_over_64kb_ends_in_truncated_marker(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'src/big.py', '')
    commit(repo, 'base')
    tag_base(repo)

    big_content = ''.join(f'line_{i:06d}\n' for i in range(6000))
    write(repo, 'src/big.py', big_content)
    commit(repo, 'huge change')

    change = slice_diff(repo, 'base')
    assert len(change.src_diff.encode('utf-8')) > 0
    assert len(change.src_diff.encode('utf-8')) <= MAX_SRC_DIFF_BYTES + len('[truncated]') + 8
    assert change.src_diff.endswith('[truncated]')


def test_binary_file_yields_no_hunk_and_binary_summary_row(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'src/pkg.py', 'x = 1\n')
    commit(repo, 'base')
    tag_base(repo)

    (repo / 'image.bin').write_bytes(bytes([0, 1, 2, 3, 0, 255, 254]))
    write(repo, 'src/pkg.py', 'x = 2\n')
    commit(repo, 'add binary file')

    change = slice_diff(repo, 'base')
    assert all(h.path != 'image.bin' for h in change.hunks)
    binary_rows = [s for s in change.summary if s.path == 'image.bin']
    assert len(binary_rows) == 1
    assert binary_rows[0].binary is True


def test_empty_diff_yields_no_hunks(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'src/pkg.py', 'x = 1\n')
    commit(repo, 'base')
    tag_base(repo)
    _git(repo, 'commit', '-q', '--allow-empty', '-m', 'noop')

    change = slice_diff(repo, 'base')
    assert change.hunks == []


def test_spaced_filename_path_is_parsed_correctly(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'my file.py', 'def helper(a: int) -> int:\n    return a\n')
    commit(repo, 'base')
    tag_base(repo)

    write(repo, 'my file.py', 'def helper(a: int) -> int:\n    return a + 1\n')
    commit(repo, 'edit spaced file')

    change = slice_diff(repo, 'base')
    assert len(change.hunks) == 1
    hunk = change.hunks[0]
    assert hunk.path == 'my file.py'
    assert hunk.after != ''
    assert hunk.neighbours.same_module_helpers != []


def test_crlf_file_preserves_line_endings(tmp_path):
    repo = make_repo(tmp_path)
    (repo / 'crlf.py').write_bytes(b'line1\r\nline2\r\nline3\r\n')
    commit(repo, 'base')
    tag_base(repo)

    (repo / 'crlf.py').write_bytes(b'line1\r\nchanged\r\nline3\r\n')
    commit(repo, 'edit crlf')

    change = slice_diff(repo, 'base')
    assert len(change.hunks) == 1
    hunk = change.hunks[0]
    assert '\r\n' in hunk.diff
    assert '\r\n' in hunk.after


def test_nonexistent_base_ref_raises_slicing_error(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, 'src/pkg.py', 'x = 1\n')
    commit(repo, 'base')

    with pytest.raises(SlicingError):
        slice_diff(repo, 'this-ref-does-not-exist')
