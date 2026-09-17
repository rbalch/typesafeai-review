"""Acceptance tests for scripts/task-status.py (the `make tasks` target).

The script shells out to `gh` for PR state; these tests cover everything before
that call — plan discovery, frontmatter parsing, and the error paths that used
to surface as tracebacks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'task-status.py'
spec = importlib.util.spec_from_file_location('task_status', SCRIPT)
assert spec is not None and spec.loader is not None
ts = importlib.util.module_from_spec(spec)
sys.modules['task_status'] = ts  # dataclasses need the module registered
spec.loader.exec_module(ts)


def write_task(plan: Path, tid: str, body: str = '') -> Path:
    plan.mkdir(parents=True, exist_ok=True)
    p = plan / f'{tid}-thing.md'
    p.write_text(
        body
        or f'---\nid: {tid}\nplan: {plan.name}\ntitle: {tid} title\ndepends_on: []\nfiles: []\nrules: []\n---\n\n## Goal\n'
    )
    return p


def test_missing_plan_dir_names_the_available_plans(tmp_path: Path) -> None:
    write_task(tmp_path / 'tasks' / 'alpha', 'T-01')
    write_task(tmp_path / 'tasks' / 'beta', 'B-01')
    with pytest.raises(SystemExit) as e:
        ts.load_plan(tmp_path / 'tasks' / 'alphabeta', tasks_root=tmp_path / 'tasks')
    msg = str(e.value)
    assert 'alphabeta' in msg and 'tasks/alpha' in msg and 'tasks/beta' in msg


def test_plan_dir_with_no_task_files_is_an_error(tmp_path: Path) -> None:
    plan = tmp_path / 'tasks' / 'empty'
    plan.mkdir(parents=True)
    (plan / 'README.md').write_text('# nothing')
    with pytest.raises(SystemExit, match='no task files'):
        ts.load_plan(plan, tasks_root=tmp_path / 'tasks')


def test_missing_frontmatter_key_names_file_and_key(tmp_path: Path) -> None:
    plan = tmp_path / 'tasks' / 'p'
    write_task(plan, 'T-01', '---\nid: T-01\ntitle: no deps key\n---\n')
    with pytest.raises(SystemExit, match=r'T-01-thing\.md.*depends_on'):
        ts.load_plan(plan, tasks_root=tmp_path / 'tasks')


def test_id_must_be_prefix_dash_number(tmp_path: Path) -> None:
    plan = tmp_path / 'tasks' / 'p'
    write_task(plan, 'T-01', '---\nid: task1\ntitle: x\ndepends_on: []\n---\n')
    with pytest.raises(SystemExit, match='task1'):
        ts.load_plan(plan, tasks_root=tmp_path / 'tasks')


def test_one_prefix_per_plan(tmp_path: Path) -> None:
    plan = tmp_path / 'tasks' / 'p'
    write_task(plan, 'T-01')
    write_task(plan, 'CT-02')
    with pytest.raises(SystemExit, match='prefix'):
        ts.load_plan(plan, tasks_root=tmp_path / 'tasks')


def test_prefix_shared_with_another_plan_is_an_error(tmp_path: Path) -> None:
    write_task(tmp_path / 'tasks' / 'alpha', 'T-01')
    write_task(tmp_path / 'tasks' / 'beta', 'T-01')
    with pytest.raises(SystemExit, match=r'prefix.*T.*alpha'):
        ts.load_plan(tmp_path / 'tasks' / 'beta', tasks_root=tmp_path / 'tasks')


def test_block_style_depends_on_parses_like_inline(tmp_path: Path) -> None:
    plan = tmp_path / 'tasks' / 'p'
    write_task(plan, 'T-01')
    write_task(plan, 'T-02', '---\nid: T-02\ntitle: b\ndepends_on:\n  - T-01\n---\n')
    write_task(plan, 'T-03', '---\nid: T-03\ntitle: c\ndepends_on: [T-01, T-02]\n---\n')
    tasks = ts.load_plan(plan, tasks_root=tmp_path / 'tasks')
    assert [t.depends_on for t in tasks] == [[], ['T-01'], ['T-01', 'T-02']]


def test_dependency_on_unknown_id_is_an_error(tmp_path: Path) -> None:
    plan = tmp_path / 'tasks' / 'p'
    write_task(plan, 'T-01', '---\nid: T-01\ntitle: a\ndepends_on: [T-09]\n---\n')
    with pytest.raises(SystemExit, match='T-09'):
        ts.load_plan(plan, tasks_root=tmp_path / 'tasks')


def test_status_derivation() -> None:
    tasks = [
        ts.Task(id='T-01', title='a', depends_on=[]),
        ts.Task(id='T-02', title='b', depends_on=['T-01']),
        ts.Task(id='T-03', title='c', depends_on=['T-02']),
        ts.Task(id='T-04', title='d', depends_on=[]),
    ]
    prs = {'T-01': {'state': 'MERGED'}, 'T-02': {'state': 'OPEN'}}
    assert ts.derive_status(tasks, prs) == {
        'T-01': 'done',
        'T-02': 'in_review',
        'T-03': 'blocked',
        'T-04': 'ready',
    }


def test_no_plan_arg_means_every_plan(tmp_path: Path) -> None:
    write_task(tmp_path / 'tasks' / 'alpha', 'T-01')
    write_task(tmp_path / 'tasks' / 'beta', 'B-01')
    (tmp_path / 'tasks' / 'README.md').write_text('# tasks')
    assert [p.name for p in ts.plan_dirs(tmp_path / 'tasks')] == ['alpha', 'beta']
