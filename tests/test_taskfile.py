"""Acceptance tests for T-02: parse planner task files (spec 4.2, 4.3)."""

import re
from pathlib import Path

import pytest

from typesafe_review.taskfile import Task, TaskFileError, _split_frontmatter, load_task, parse_task

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


def test_backtick_leading_title_parses_to_literal_string(tmp_path: Path):
    # Unquoted, a leading backtick is a reserved YAML indicator and pyyaml refuses to
    # scan it as a plain scalar (RA-02b; six real task files hit this).
    p = tmp_path / 'bad.md'
    p.write_text('---\nid: T-99\ntitle: `--task` accepts a PR number or URL\n---\n\n## Acceptance\n\n- x\n')
    task = load_task(p)
    assert task.title == '`--task` accepts a PR number or URL'


def test_backtick_leading_value_with_inner_double_quote_survives(tmp_path: Path):
    p = tmp_path / 'bad.md'
    p.write_text('---\nid: T-99\ntitle: `say "hi"` to the user\n---\n\n## Acceptance\n\n- x\n')
    task = load_task(p)
    assert task.title == '`say "hi"` to the user'


def test_backtick_leading_value_with_backslash_and_double_quote_survives(tmp_path: Path):
    # `_quote_backtick_values` wraps the value in double quotes; an unescaped `\`
    # ahead of the `"` escape it adds turns `\"` into an escaped quote (or worse) in
    # the eyes of the YAML scanner, instead of a literal backslash followed by a
    # closing quote. Backslash must be escaped first, then `"`.
    p = tmp_path / 'bad.md'
    p.write_text('---\nid: T-99\ntitle: `re.sub(r"\\d")` strips digits\n---\n\n## Acceptance\n\n- x\n')
    task = load_task(p)
    assert task.title == '`re.sub(r"\\d")` strips digits'


def test_list_item_starting_with_backtick_is_never_quoted_and_stays_invalid_yaml():
    """`_quote_backtick_values` only rewrites unindented `key: value` lines --
    `_TOP_LEVEL_LINE_RE` is anchored with no leading whitespace precisely so a
    `files:`/`rules:` list item is never touched. A list item that itself starts
    with an unquoted backtick is therefore left exactly as authored, which means it
    is still invalid YAML (the same reserved-indicator problem a bare `title:`
    value has) -- `_split_frontmatter` surfaces that as `TaskFileError`, not a
    silently-fixed list."""
    text = '---\nid: T-99\ntitle: fine\nfiles:\n  - `src/foo.py`\n---\n\n## Acceptance\n\n- x\n'
    with pytest.raises(TaskFileError):
        _split_frontmatter(text, 'src')


def test_list_item_with_nested_key_and_backtick_value_is_never_touched():
    """A `files:` item shaped like its own `key: value` pair, whose value starts
    with a backtick (`` - urgent: `fix bug` ``), is genuinely invalid YAML on its
    own -- pyyaml hits the same reserved-backtick-indicator problem there as it
    does for a bare `title:` value -- and `_quote_backtick_values` must leave it
    alone rather than "fix" it, because it isn't a top-level line. If the anchor in
    `_TOP_LEVEL_LINE_RE` were ever loosened to also match an indented `- key:
    value` line, this item would stop raising and instead silently corrupt the
    frontmatter (`files` becomes empty, a spurious top-level `urgent` key appears)
    -- see the round-2 report for the mutation that was run to confirm this test
    catches exactly that."""
    text = '---\nid: T-99\ntitle: fine\nfiles:\n  - urgent: `fix bug`\n---\n\n## Acceptance\n\n- x\n'
    with pytest.raises(TaskFileError):
        _split_frontmatter(text, 'src')


def test_list_item_containing_colon_space_is_never_quoted():
    """A `files:` list item containing `: ` is left exactly as authored too --
    pyyaml reads it as its own nested one-entry mapping (a dict, not a quoted
    string), which is exactly what happens with no `_quote_backtick_values`
    involved at all. If a list-item line were being quoted the same way a top-level
    value is, this would come back as the literal string `'note: keep this as a
    nested mapping'` instead of a dict."""
    text = '---\nid: T-99\ntitle: fine\nfiles:\n  - note: keep this as a nested mapping\n---\n\n## Acceptance\n\n- x\n'
    frontmatter, _ = _split_frontmatter(text, 'src')
    assert frontmatter['files'] == [{'note': 'keep this as a nested mapping'}]


def test_malformed_frontmatter_raises_taskfileerror_never_raw_yaml_error(tmp_path: Path):
    p = tmp_path / 'bad.md'
    # Genuinely broken YAML (unterminated flow sequence), not just an unquoted
    # backtick -- quoting a backtick-leading value must not paper over this.
    p.write_text('---\nid: T-99\ntitle: [unterminated\n---\n\n## Acceptance\n\n- x\n')
    with pytest.raises(TaskFileError):
        load_task(p)


def test_outer_blank_lines_do_not_change_the_parsed_task():
    """RA-02 fix round 2: a `Task` (in particular `criteria_text`, which feeds the
    change-wide state hash) must be identical whether `text` came from a file
    exactly as written, or from a PR body brief that `prsource.extract_brief` pads
    with -- or strips down to -- extra leading/trailing blank lines.
    """
    text = EXAMPLE_FIXTURE.read_text()
    assert parse_task(text, 'a') == parse_task('\n\n' + text + '\n\n', 'a')
