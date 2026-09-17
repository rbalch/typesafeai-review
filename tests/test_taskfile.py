"""Acceptance tests for T-02: parse planner task files (spec 4.2, 4.3)."""

import re
from pathlib import Path

import pytest

from typesafe_review.taskfile import Task, TaskFileError, load_task

FIXTURE_DIR = Path(__file__).parent / 'fixtures' / 'tasks'
EXAMPLE_FIXTURE = FIXTURE_DIR / 'T-02-example.md'


def _expected_criteria_text() -> str:
    """Slice the fixture's `## Acceptance` body without using the implementation."""
    text = EXAMPLE_FIXTURE.read_text()
    start = text.index('## Acceptance\n') + len('## Acceptance\n')
    end = text.index('\n## Context')
    return text[start : end + 1]


def test_readme_example_parses_to_two_criteria():
    task = load_task(EXAMPLE_FIXTURE)
    assert isinstance(task, Task)
    assert task.id == 'T-02'
    assert task.title == 'Add the repository layer for orders'
    assert len(task.acceptance) == 2


def test_multiline_bullet_joins_with_space():
    task = load_task(EXAMPLE_FIXTURE)
    first = task.acceptance[0]
    assert 'covers: create, get by id, get missing raises `OrderNotFound`' in first
    assert '\n' not in first


def test_criteria_text_is_byte_identical_to_section_body():
    task = load_task(EXAMPLE_FIXTURE)
    assert task.criteria_text == _expected_criteria_text()


def test_missing_acceptance_section_raises(tmp_path: Path):
    p = tmp_path / 'bad.md'
    p.write_text('---\nid: T-99\ntitle: No acceptance section\n---\n\n## Goal\n\nSomething.\n')
    with pytest.raises(TaskFileError):
        load_task(p)


def test_empty_bullet_list_raises(tmp_path: Path):
    p = tmp_path / 'bad.md'
    p.write_text(
        '---\nid: T-99\ntitle: Empty acceptance\n---\n\n'
        '## Acceptance\n\nNo bullets here, just prose.\n\n## Context\n\nNone.\n'
    )
    with pytest.raises(TaskFileError):
        load_task(p)


def test_missing_file_raises_taskfileerror(tmp_path: Path):
    p = tmp_path / 'does-not-exist.md'
    with pytest.raises(TaskFileError, match=re.escape(str(p))):
        load_task(p)


def test_no_frontmatter_raises(tmp_path: Path):
    p = tmp_path / 'bad.md'
    p.write_text('## Goal\n\nNo frontmatter at all.\n')
    with pytest.raises(TaskFileError):
        load_task(p)


def test_missing_id_raises(tmp_path: Path):
    p = tmp_path / 'bad.md'
    p.write_text('---\ntitle: Missing id\n---\n\n## Acceptance\n\n- do the thing\n')
    with pytest.raises(TaskFileError):
        load_task(p)


def test_missing_title_raises(tmp_path: Path):
    p = tmp_path / 'bad.md'
    p.write_text('---\nid: T-99\n---\n\n## Acceptance\n\n- do the thing\n')
    with pytest.raises(TaskFileError):
        load_task(p)
