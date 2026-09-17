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


def load_acceptance_tests(worktree: Path, red_sha: str | None) -> str:
    """Whole contents of every file under `tests/` as it read at `red_sha`, joined
    with a blank line. No red sha → `""` (spec §4.2 item 3).
    """
    if red_sha is None:
        return ''

    listing = _run_git(worktree, ['ls-tree', '-r', '--name-only', '-z', red_sha, '--', 'tests/'])
    paths = [p for p in listing.decode('utf-8', errors='replace').split('\0') if p]

    contents: list[str] = []
    for path in paths:
        raw = _run_git(worktree, ['show', f'{red_sha}:{path}'])
        contents.append(raw.decode('utf-8', errors='replace'))
    return '\n\n'.join(contents)


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
