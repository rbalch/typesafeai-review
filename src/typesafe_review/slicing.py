"""Slice `base..HEAD` into per-hunk state and change-wide summaries.

Pure code, no model. See spec sections 4.2, 4.3, 4.4. `slice_diff` is the entry
point; everything else is a helper for it.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# spec §4.4: hunks over this many diff lines are truncated, never silently dropped.
MAX_HUNK_LINES = 400
# spec §4.3: src_diff is capped and marked; test_diff is not (caller decides).
MAX_SRC_DIFF_BYTES = 64 * 1024

_HUNK_HEADER_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$')
_DEF_NAME_RE = re.compile(r'^\s*(?:async\s+)?def\s+(\w+)')
_CLASS_NAME_RE = re.compile(r'^\s*class\s+(\w+)')
_TEST_BASENAME_RE = re.compile(r'^(test_.*\.py|.*_test\.py)$')
_CONTEXT_LINES = 20


class SlicingError(RuntimeError):
    """A git subprocess or file read needed for slicing failed.

    Never swallowed: a slicing failure must not produce an empty Change that looks
    like "nothing changed".
    """


@dataclass
class Neighbours:
    same_module_helpers: list[str] = field(default_factory=list)
    tests_touching_file: list[str] = field(default_factory=list)


@dataclass
class Hunk:
    path: str
    header: str
    diff: str
    after: str
    symbol: str
    language: str
    is_test: bool
    is_new: bool
    is_deleted_only: bool
    truncated: bool
    neighbours: Neighbours


@dataclass
class FileSummary:
    path: str
    added: int
    removed: int
    is_test: bool
    binary: bool = False


@dataclass
class Change:
    hunks: list[Hunk]
    summary: list[FileSummary]
    src_diff: str
    test_diff: str


def _git_env() -> dict[str, str]:
    # Stable messages and sort order; a locale dependency here would make slicing
    # non-deterministic across machines.
    return {**os.environ, 'LC_ALL': 'C'}


def _git_cmd(worktree: Path, attrs_path: Path, args: list[str]) -> list[str]:
    return [
        'git',
        '-C',
        str(worktree),
        '-c',
        f'core.attributesFile={attrs_path}',
        # Paths with spaces or non-ASCII bytes must come back unquoted so path
        # parsing does not have to unescape C-style quoting.
        '-c',
        'core.quotePath=false',
        *args,
    ]


def _run_git(worktree: Path, attrs_path: Path, args: list[str]) -> bytes:
    """Run git, capturing raw bytes. Text mode would translate/strip `\\r`,
    corrupting CRLF file content carried inside diff and show output."""
    cmd = _git_cmd(worktree, attrs_path, args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=False, env=_git_env(), check=False)
    except OSError as exc:
        raise SlicingError(f'failed to run `{" ".join(cmd)}`: {exc}') from exc
    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace').strip()
        raise SlicingError(f'`{" ".join(cmd)}` failed: {stderr}')
    return result.stdout


def _decode(data: bytes) -> str:
    return data.decode('utf-8', errors='replace')


def _file_exists_at_head(worktree: Path, attrs_path: Path, path: str) -> bool:
    """Probe existence with `git cat-file -e`, never by sniffing stderr text."""
    cmd = _git_cmd(worktree, attrs_path, ['cat-file', '-e', f'HEAD:{path}'])
    try:
        result = subprocess.run(cmd, capture_output=True, text=False, env=_git_env(), check=False)
    except OSError as exc:
        raise SlicingError(f'failed to run `{" ".join(cmd)}`: {exc}') from exc
    return result.returncode == 0


def _show_file_at_head(worktree: Path, attrs_path: Path, path: str, added_by_path: dict[str, int]) -> str | None:
    """`git show HEAD:<path>`, or None if the path is a recorded deletion.

    Existence is decided by `git cat-file -e`, not by matching stderr text. If
    the probe says the path exists, `git show` must succeed too; any failure at
    that point is unexpected and raises `SlicingError`. If the probe says the
    path is missing, that is only an expected outcome when the numstat summary
    records it as a deletion (`added == 0`); any other missing path raises
    `SlicingError` rather than silently returning empty context.
    """
    if _file_exists_at_head(worktree, attrs_path, path):
        return _decode(_run_git(worktree, attrs_path, ['show', f'HEAD:{path}']))
    if added_by_path.get(path) == 0:
        return None
    raise SlicingError(f'HEAD:{path} does not exist and is not a recorded deletion in the diff summary')


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    if 'tests' in parts[:-1]:
        return True
    return bool(_TEST_BASENAME_RE.match(Path(path).name))


def _language(path: str) -> str:
    ext = Path(path).suffix
    if ext == '.py':
        return 'python'
    return ext[1:] if ext else ''


def symbol_from_header(header: str) -> str:
    """The function or class name a raw `@@ ... @@` hunk header line names, or
    `<module>`. Public so callers with only the header text (e.g. `compose.py`,
    working from `HunkState`, which does not carry `Hunk.symbol`) can derive the
    same symbol `slicing.py` itself computes when it builds a `Hunk`.
    """
    match = _HUNK_HEADER_RE.match(header)
    funcname = match.group(5) if match else header
    return _symbol(funcname)


def _symbol(funcname: str) -> str:
    funcname = funcname.strip()
    if not funcname:
        return '<module>'
    m = _DEF_NAME_RE.match(funcname)
    if m:
        return m.group(1)
    m = _CLASS_NAME_RE.match(funcname)
    if m:
        return m.group(1)
    return funcname


def _parse_numstat(output: str) -> list[FileSummary]:
    rows: list[FileSummary] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        added_s, removed_s, path = line.split('\t', 2)
        binary = added_s == '-' or removed_s == '-'
        added = 0 if binary else int(added_s)
        removed = 0 if binary else int(removed_s)
        rows.append(FileSummary(path=path, added=added, removed=removed, is_test=_is_test_path(path), binary=binary))
    return rows


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    node_type = ast.AsyncFunctionDef if isinstance(node, ast.AsyncFunctionDef) else ast.FunctionDef
    stub = node_type(
        name=node.name,
        args=node.args,
        body=[ast.Pass()],
        decorator_list=[],
        returns=node.returns,
        type_comment=None,
        type_params=getattr(node, 'type_params', []),
    )
    ast.fix_missing_locations(stub)
    source = ast.unparse(stub)
    first_line = source.splitlines()[0]
    return first_line.rstrip(':')


def _same_module_helpers(content: str | None) -> list[str]:
    if content is None:
        return []
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    signatures = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures.append(_function_signature(node))
    return signatures


def _splitlines_keepends(text: str) -> list[str]:
    """Split on `\\n` only, keeping terminators.

    Unlike `str.splitlines(keepends=True)`, a lone `\\r` is not a line boundary,
    so a `\\r` carried by a CRLF file's content stays attached to its line
    instead of being treated as a separate empty line.
    """
    if text == '':
        return []
    parts = text.split('\n')
    ends_with_newline = parts[-1] == ''
    if ends_with_newline:
        parts = parts[:-1]
    lines = [p + '\n' for p in parts]
    if not ends_with_newline and lines:
        lines[-1] = lines[-1][:-1]
    return lines


def _split_lines_no_terminators(text: str) -> list[str]:
    """Split on `\\n` only, dropping the trailing empty element if any."""
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return lines


def _file_window(content: str | None, new_start: int, new_count: int) -> str:
    if content is None:
        return ''
    lines = _splitlines_keepends(content)
    if new_count <= 0:
        start_idx = max(new_start - 1, 0)
        end_idx = start_idx
    else:
        start_idx = new_start - 1
        end_idx = start_idx + new_count - 1
    window_start = max(start_idx - _CONTEXT_LINES, 0)
    window_end = min(end_idx + _CONTEXT_LINES, len(lines) - 1)
    if window_end < window_start:
        return ''
    return ''.join(lines[window_start : window_end + 1])


def _split_file_blocks(diff_text: str) -> list[str]:
    if not diff_text.strip():
        return []
    blocks = re.split(r'(?=^diff --git )', diff_text, flags=re.MULTILINE)
    return [b for b in blocks if b.strip()]


def _block_path(block: str) -> tuple[str, bool]:
    """Return (path, is_new) for a `diff --git` block, using +++/--- lines.

    `--no-renames` guarantees a block is either an add, a delete, or a same-path
    modification, never a rename.
    """
    old_path = None
    new_path = None
    for line in _split_lines_no_terminators(block):
        if line.startswith('--- '):
            old_path = line[4:]
        elif line.startswith('+++ '):
            new_path = line[4:]
            break
    is_new = old_path == '/dev/null'
    if new_path and new_path != '/dev/null':
        path = new_path.removeprefix('b/')
    elif old_path and old_path != '/dev/null':
        path = old_path.removeprefix('a/')
    else:
        path = ''
    # Git appends a bare trailing tab to `---`/`+++` paths that contain a space,
    # to disambiguate the path from the rest of the line.
    path = path.removesuffix('\t')
    return path, is_new


def _test_paths_and_contents(worktree: Path, attrs_path: Path) -> dict[str, str]:
    listing = _decode(_run_git(worktree, attrs_path, ['ls-tree', '-r', '--name-only', 'HEAD']))
    test_paths = [p for p in _split_lines_no_terminators(listing) if p and _is_test_path(p)]
    contents: dict[str, str] = {}
    for path in test_paths:
        # `git ls-tree HEAD` only lists paths that exist at HEAD, so there is no
        # legitimate deletion case here; a missing show is a `SlicingError`.
        text = _show_file_at_head(worktree, attrs_path, path, added_by_path={})
        contents[path] = text or ''
    return contents


def _tests_touching_file(path: str, test_contents: dict[str, str]) -> list[str]:
    stem = Path(path).stem
    if not stem:
        return []
    pattern = re.compile(r'\b' + re.escape(stem) + r'\b')
    return [test_path for test_path, content in test_contents.items() if pattern.search(content)]


def _parse_hunks_for_block(
    block: str,
    path: str,
    is_new: bool,
    is_test: bool,
    file_content: str | None,
    helpers: list[str],
    touching_tests: list[str],
) -> list[Hunk]:
    hunks: list[Hunk] = []
    lines = _split_lines_no_terminators(block)
    i = 0
    n = len(lines)
    while i < n:
        m = _HUNK_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        header = lines[i]
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        funcname = m.group(5)
        i += 1
        body: list[str] = []
        while i < n and not _HUNK_HEADER_RE.match(lines[i]):
            body.append(lines[i])
            i += 1

        truncated = len(body) > MAX_HUNK_LINES
        if truncated:
            body = body[:MAX_HUNK_LINES]
        diff_text = '\n'.join([header, *body])

        is_deleted_only = new_count == 0
        after = _file_window(file_content, new_start, new_count)

        hunks.append(
            Hunk(
                path=path,
                header=header,
                diff=diff_text,
                after=after,
                symbol=_symbol(funcname),
                language=_language(path),
                is_test=is_test,
                is_new=is_new,
                is_deleted_only=is_deleted_only,
                truncated=truncated,
                neighbours=Neighbours(
                    same_module_helpers=helpers,
                    tests_touching_file=touching_tests,
                ),
            )
        )
    return hunks


def _split_diff(full_diff: str, is_test_by_path: dict[str, bool]) -> tuple[str, str]:
    src_parts: list[str] = []
    test_parts: list[str] = []
    for block in _split_file_blocks(full_diff):
        path, _ = _block_path(block)
        if is_test_by_path.get(path, _is_test_path(path)):
            test_parts.append(block)
        else:
            src_parts.append(block)
    src_diff = ''.join(src_parts)
    test_diff = ''.join(test_parts)

    src_bytes = src_diff.encode('utf-8')
    if len(src_bytes) > MAX_SRC_DIFF_BYTES:
        marker = '[truncated]'
        truncated_bytes = src_bytes[:MAX_SRC_DIFF_BYTES]
        src_diff = truncated_bytes.decode('utf-8', errors='ignore') + '\n' + marker

    return src_diff, test_diff


def slice_diff(worktree: Path, base: str) -> Change:
    """Slice `git diff base..HEAD` in `worktree` into hunks and summaries.

    Raises `SlicingError` if any git subprocess this needs fails; never returns
    an empty `Change` to paper over a failure.
    """
    worktree = Path(worktree)
    with tempfile.NamedTemporaryFile('w', suffix='.gitattributes', delete=False) as attrs_file:
        attrs_file.write('*.py diff=python\n')
        attrs_path = Path(attrs_file.name)

    try:
        numstat_out = _decode(_run_git(worktree, attrs_path, ['diff', '--no-renames', '--numstat', f'{base}..HEAD']))
        summary = _parse_numstat(numstat_out)

        if not summary:
            return Change(hunks=[], summary=[], src_diff='', test_diff='')

        is_test_by_path = {s.path: s.is_test for s in summary}
        binary_paths = {s.path for s in summary if s.binary}
        added_by_path = {s.path: s.added for s in summary}

        unified_out = _decode(_run_git(worktree, attrs_path, ['diff', '--no-renames', '--unified=0', f'{base}..HEAD']))
        blocks = _split_file_blocks(unified_out)

        file_content_cache: dict[str, str | None] = {}
        helpers_cache: dict[str, list[str]] = {}
        test_contents = _test_paths_and_contents(worktree, attrs_path)

        hunks: list[Hunk] = []
        for block in blocks:
            path, is_new = _block_path(block)
            if not path or path in binary_paths:
                continue
            is_test = is_test_by_path.get(path, _is_test_path(path))

            if path not in file_content_cache:
                file_content_cache[path] = _show_file_at_head(worktree, attrs_path, path, added_by_path)
            file_content = file_content_cache[path]

            if path not in helpers_cache:
                helpers_cache[path] = _same_module_helpers(file_content) if _language(path) == 'python' else []
            helpers = helpers_cache[path]

            touching_tests = _tests_touching_file(path, test_contents)

            hunks.extend(_parse_hunks_for_block(block, path, is_new, is_test, file_content, helpers, touching_tests))

        full_diff = _decode(_run_git(worktree, attrs_path, ['diff', '--no-renames', f'{base}..HEAD']))
        src_diff, test_diff = _split_diff(full_diff, is_test_by_path)

        return Change(hunks=hunks, summary=summary, src_diff=src_diff, test_diff=test_diff)
    finally:
        attrs_path.unlink(missing_ok=True)
