"""Build the two JSON state shapes Jev sees (spec §4.2, §4.3).

Pure code, no model call. `build_hunk_states` and `build_change_state` turn a `Task`
(or `None`) and a `Change` (from `slicing.py`) into the exact `TypedDict` shapes the
spec defines. `load_conventions` and `load_acceptance_tests` gather the two pieces
that are not already on `Task` or `Change`: the worktree's `AGENTS.md` shape sections,
and the acceptance test files as they read at the red commit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TypedDict

from typesafe_review.slicing import Change
from typesafe_review.taskfile import Task

#: spec §4.2 item 6: a hunk state larger than this is a bug (or a hunk that needs
#: truncating harder), never something to send anyway.
MAX_HUNK_STATE_BYTES = 1024 * 1024

#: RA-06: the soft per-request budget `pipeline._build_requests` checks before
#: sending anything to Jev, in `pipeline._estimate_tokens` units (bytes / 4).
#: Measured, not guessed. 2026-09-18 repro (tasks/run-anywhere/RA-06-state-budget.md
#: Context): largest recorded success 2,842 input tokens; an untruncated `tests/`
#: dump at ~49,265 tokens got `400 {"error_type":"max_tokens_exceeded"}`. The
#: first calibration point, 8,000, skipped PR #15's change-wide request (13,410
#: estimated) and `hunk-27` (8,597), so 2026-09-21 probed the ceiling with one
#: Noul against a padded state, `usage.input_tokens` from each success:
#:
#:   padding          estimated  result  input_tokens
#:   real PR-15 diff     13,513  ok            15,557
#:   real PR-15 diff     16,501  ok            19,059
#:   real PR-15 diff     25,008  ok            28,767
#:   real PR-15 diff     29,010  400 max_tokens_exceeded
#:   synthetic numeric   15,014  ok            29,540
#:   synthetic numeric   18,003  400 max_tokens_exceeded
#:
#: So the API caps a request at roughly 32K real input tokens, and the bytes/4
#: estimate undercounts by ~1.15x on real code and ~2x on number-heavy text.
#: 16,000 keeps the worst measured ratio (16,000 x 2 = 32K) at the ceiling and
#: real code (~18.4K) well under it, while clearing every request PR #15 needs.
MAX_REQUEST_TOKENS = 16000

# spec §4.2: conventions are exactly these three `AGENTS.md` sections, in this order,
# verbatim, joined with a blank line. A missing section contributes nothing.
_CONVENTION_HEADINGS = ('## Architectural shape', '## Always', '## Never')


class StateError(Exception):
    """A git subprocess or file read needed to build state failed, or a hunk state
    exceeded the size cap.

    Never swallowed: every file read and subprocess call this module makes is wrapped
    so no bare `FileNotFoundError` or `OSError` escapes to the caller.
    """


class TaskState(TypedDict):
    id: str
    title: str
    acceptance: list[str]


class ChangeTaskState(TypedDict):
    id: str
    title: str
    acceptance: list[str]
    criteria_text: str


class FileState(TypedDict):
    path: str
    language: str
    is_test: bool
    is_new: bool


class HunkInfoState(TypedDict):
    header: str
    diff: str
    after: str


class NeighboursState(TypedDict):
    same_module_helpers: list[str]
    tests_touching_file: list[str]


class HunkState(TypedDict):
    task: TaskState | None
    file: FileState
    hunk: HunkInfoState
    neighbours: NeighboursState
    conventions: str


class FileSummaryState(TypedDict):
    path: str
    added: int
    removed: int
    is_test: bool


class ChangeState(TypedDict):
    task: ChangeTaskState | None
    diff_summary: list[FileSummaryState]
    src_diff: str
    test_diff: str
    acceptance_tests: str


def _extract_heading_section(lines: list[str], heading: str) -> str | None:
    """Text strictly between `heading`'s own line and the next `## ` line, or `None`
    if `heading` is not present. Trailing blank lines are trimmed so a caller can
    join sections with its own blank-line separator without doubling them up.
    """
    start = None
    for i, line in enumerate(lines):
        if line.rstrip('\n') == heading:
            start = i + 1
            break
    if start is None:
        return None

    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith('## '):
            end = i
            break

    section = ''.join(lines[start:end]).strip('\n')
    return section or None


def load_conventions(worktree: Path) -> str:
    """`AGENTS.md`'s 'Architectural shape' + 'Always' + 'Never' sections, verbatim,
    joined with blank lines. A wholly missing `AGENTS.md` is the only way to get `""`;
    a present file missing one or more of the three sections just contributes less.
    """
    agents_path = worktree / 'AGENTS.md'
    try:
        text = agents_path.read_text()
    except FileNotFoundError:
        return ''
    except OSError as e:
        raise StateError(f'{agents_path}: cannot read AGENTS.md ({e.strerror or e})') from e

    lines = text.splitlines(keepends=True)
    sections = [
        section for heading in _CONVENTION_HEADINGS if (section := _extract_heading_section(lines, heading)) is not None
    ]
    return '\n\n'.join(sections)


def _run_git(worktree: Path, args: list[str]) -> bytes:
    """Run git for state-gathering, capturing raw bytes (no `text=True`) so CRLF test
    file content survives untouched. Reimplemented here rather than imported from
    `checks.py`, whose git helpers are private to that module.
    """
    cmd = ['git', '-C', str(worktree), *args]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as e:
        raise StateError(f'failed to run `{" ".join(cmd)}`: {e}') from e
    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace').strip()
        raise StateError(f'`{" ".join(cmd)}` failed: {stderr}')
    return result.stdout


#: The tree git diffs a root commit (no parent) against: `git hash-object -t tree
#: /dev/null`, the same well-known empty-tree sha `git diff --root` / GitHub both use.
#: (RA-06 brief context: the brief itself quotes this sha with one extra `0` --
#: `4b825dc642cb6eb9a0060e54bf8d69288fbee4904`, 41 hex chars, not a valid SHA-1;
#: this is the actual 40-char value `git hash-object -t tree /dev/null` prints.)
_EMPTY_TREE_SHA = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'


def _parent_ref(worktree: Path, red_sha: str) -> str:
    """`<red_sha>^`, or `_EMPTY_TREE_SHA` if `red_sha` has no parent (a root
    commit). `rev-parse --verify` failing here is an expected "no parent" signal,
    not a git failure, so unlike `_run_git` a nonzero exit never raises."""
    cmd = ['git', '-C', str(worktree), 'rev-parse', '--verify', f'{red_sha}^']
    try:
        result = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as e:
        raise StateError(f'failed to run `{" ".join(cmd)}`: {e}') from e
    if result.returncode == 0:
        return f'{red_sha}^'
    return _EMPTY_TREE_SHA


def _added_python_lines(diff_text: str) -> str:
    """Every `+` line of every `*.py` file in a unified diff, grouped back per file
    and joined with a blank line -- `__init__.py` and any path under a `fixtures/`
    directory contribute nothing. Parses only `+++ b/<path>` headers to decide which
    file the following `+` lines belong to (never `diff --git`, whose two paths are
    ambiguous to split on a renamed/spaced path) and strips a trailing tab off that
    header before reading the path (ledger F-5: `git diff` appends a tab when the
    path needs quoting)."""
    per_file: list[str] = []
    current_lines: list[str] | None = None

    def _flush() -> None:
        if current_lines:
            per_file.append('\n'.join(current_lines))

    for line in diff_text.splitlines():
        if line.startswith('+++ '):
            _flush()
            current_lines = None
            raw_path = line[len('+++ ') :].split('\t', 1)[0]
            if raw_path == '/dev/null':
                continue
            path = raw_path.removeprefix('b/')
            if path.endswith('.py') and Path(path).name != '__init__.py' and 'fixtures/' not in path:
                current_lines = []
            continue
        if current_lines is not None and line.startswith('+') and not line.startswith('+++'):
            current_lines.append(line[1:])
    _flush()
    return '\n\n'.join(per_file)


def load_acceptance_tests(worktree: Path, red_sha: str | None) -> str:
    """The red commit's own diff under `tests/`, restricted to added/modified lines
    of `*.py` files (spec §4.2 item 3, §4.3: "the test functions committed at
    red-sha"). No red sha → `""`. Fixtures, data files and `__init__.py` never
    contribute, and a file the red commit never touched never appears at all --
    unlike the whole-file dump this replaced, `acceptance_tests` here can never be
    bigger than the red commit's own diff.
    """
    if red_sha is None:
        return ''

    parent = _parent_ref(worktree, red_sha)
    raw = _run_git(worktree, ['diff', parent, red_sha, '--', 'tests/'])
    return _added_python_lines(raw.decode('utf-8', errors='replace'))


def _task_state(task: Task | None) -> TaskState | None:
    if task is None:
        return None
    return {'id': task.id, 'title': task.title, 'acceptance': list(task.acceptance)}


def _change_task_state(task: Task | None) -> ChangeTaskState | None:
    if task is None:
        return None
    return {
        'id': task.id,
        'title': task.title,
        'acceptance': list(task.acceptance),
        'criteria_text': task.criteria_text,
    }


def _check_size(state: HunkState, name: str) -> None:
    """Raise `StateError` naming `name` if `state` is not JSON-serialisable or if it
    serialises to more than `MAX_HUNK_STATE_BYTES`.
    """
    try:
        encoded = json.dumps(state)
    except (TypeError, ValueError) as e:
        raise StateError(f'{name}: hunk state is not JSON-serialisable ({e})') from e
    size = len(encoded.encode('utf-8'))
    if size > MAX_HUNK_STATE_BYTES:
        raise StateError(f'{name}: hunk state is {size} bytes, over the {MAX_HUNK_STATE_BYTES} byte cap')


def build_hunk_states(task: Task | None, change: Change, conventions: str) -> list[HunkState]:
    """One `HunkState` per hunk in `change`, in order. Raises `StateError` naming the
    hunk if any single hunk's state exceeds the size cap.
    """
    task_state = _task_state(task)
    states: list[HunkState] = []
    for hunk in change.hunks:
        state: HunkState = {
            'task': task_state,
            'file': {
                'path': hunk.path,
                'language': hunk.language,
                'is_test': hunk.is_test,
                'is_new': hunk.is_new,
            },
            'hunk': {
                'header': hunk.header,
                'diff': hunk.diff,
                'after': hunk.after,
            },
            'neighbours': {
                'same_module_helpers': list(hunk.neighbours.same_module_helpers),
                'tests_touching_file': list(hunk.neighbours.tests_touching_file),
            },
            'conventions': conventions,
        }
        _check_size(state, f'{hunk.path} {hunk.header}')
        states.append(state)
    return states


def build_change_state(task: Task | None, change: Change, acceptance_tests: str) -> ChangeState:
    """The single change-wide `ChangeState` (spec §4.3)."""
    return {
        'task': _change_task_state(task),
        'diff_summary': [
            {'path': s.path, 'added': s.added, 'removed': s.removed, 'is_test': s.is_test} for s in change.summary
        ],
        'src_diff': change.src_diff,
        'test_diff': change.test_diff,
        'acceptance_tests': acceptance_tests,
    }
