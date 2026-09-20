"""Parse a planner task file into the `task` state block (spec 4.2, 4.3).

Only the pieces the reviewer needs are read: `id`, `title`, and the `## Acceptance`
section. Everything else in a task file (`status`, `depends_on`, `files`, `rules`,
`## Goal`, etc.) is the planner's and orchestrator's concern, not this reviewer's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_ACCEPTANCE_HEADING = '## Acceptance'

#: A top-level `key: value` frontmatter line, with no leading whitespace, so list
#: items (`  - \`foo\``) under a `files:`/`rules:` block are never matched.
_TOP_LEVEL_LINE_RE = re.compile(r'^([A-Za-z0-9_-]+:[ \t]*)(.*)$')


class TaskFileError(Exception):
    """A task file is missing or malformed.

    Raised instead of returning an empty or partial `Task`: a task file that cannot be
    fully parsed is a hard failure, never a silent default.
    """


@dataclass(frozen=True)
class Task:
    """The subset of a planner task file the reviewer's state shapes need."""

    id: str
    title: str
    acceptance: list[str]
    criteria_text: str


def load_task(path: Path) -> Task:
    """Read `path` and parse it into a `Task`, or raise `TaskFileError` naming what
    is missing. The file read is the only thing this wraps; parsing itself is
    `parse_task`, shared with `prsource.py` (RA-02), which parses a PR body brief
    that was never a file on disk."""
    try:
        text = path.read_text()
    except OSError as e:
        raise TaskFileError(f'{path}: cannot read task file ({e.strerror or e})') from e
    return parse_task(text, str(path))


def parse_task(text: str, source: str) -> Task:
    """Parse `text` into a `Task`, or raise `TaskFileError` naming what is missing.

    `source` is a label used only in error messages -- a file path for `load_task`,
    or e.g. `'PR #13'` for a brief pulled out of a PR body (RA-02).

    `text` is normalised to a single trailing newline (and no leading blank lines)
    before anything else runs, so the same task text produces the same `Task` --
    byte for byte, including `criteria_text`, which feeds the change-wide state hash
    -- whether it came from a file on disk or a PR body brief that
    `prsource.extract_brief` has already stripped of its own outer blank lines
    (RA-02 fix round 2: a file-sourced and a PR-sourced `Task` for the same text
    must hash to the same request key, or every recorded fixture becomes source-
    dependent). This only trims *outer* blank lines; blank lines inside a section
    are still kept verbatim, per spec 4.3.
    """
    text = text.strip('\n') + '\n'
    frontmatter, body = _split_frontmatter(text, source)

    task_id = frontmatter.get('id')
    if not task_id:
        raise TaskFileError(f"{source}: frontmatter is missing 'id'")

    title = frontmatter.get('title')
    if not title:
        raise TaskFileError(f"{source}: frontmatter is missing 'title'")

    criteria_text = _extract_section(body, _ACCEPTANCE_HEADING, source)
    acceptance = _extract_bullets(criteria_text, source)

    return Task(id=str(task_id), title=str(title), acceptance=acceptance, criteria_text=criteria_text)


def _split_frontmatter(text: str, source: str) -> tuple[dict, str]:
    """Split `text` into (parsed frontmatter, body) at the first two `---` lines."""
    lines = text.split('\n')
    if not lines or lines[0].strip() != '---':
        raise TaskFileError(f'{source}: no frontmatter found (file must start with `---`)')

    end_index = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == '---':
            end_index = i
            break
    if end_index is None:
        raise TaskFileError(f'{source}: no frontmatter found (no closing `---`)')

    frontmatter_text = '\n'.join(lines[1:end_index])
    body = '\n'.join(lines[end_index + 1 :])

    try:
        data = yaml.safe_load(_quote_backtick_values(frontmatter_text))
    except yaml.YAMLError as e:
        raise TaskFileError(f'{source}: frontmatter is not valid YAML ({e})') from e
    if not isinstance(data, dict):
        raise TaskFileError(f'{source}: frontmatter did not parse to a mapping')
    return data, body


def _quote_backtick_values(frontmatter_text: str) -> str:
    """Wrap a top-level `key: value` line's value in double quotes when the value,
    left unquoted, is not valid YAML: it starts with a backtick (a reserved
    indicator pyyaml refuses to scan, `` title: `--task` accepts … ``), or it
    contains `: ` further in (read as a nested mapping key by a YAML parser,
    `` title: Review any ref: `--pr` … ``). Any double quote already in the value is
    escaped. Values that already parse fine as a plain scalar (no colon, no leading
    backtick) or that are already a flow sequence/mapping/quoted string (`[...]`,
    `{...}`, `"..."`, `'...'`) pass through unchanged -- deliberately, so a `files:`
    or `depends_on:` list value is never touched.

    Broader than a literal reading of "value starts with a backtick" (RA-02b scope
    item 3): two of the six task files it names (`T-04`, `RA-03`) fail today for the
    colon-in-value reason, not a leading backtick, so a backtick-only quote would
    leave them broken and the task's own round-trip acceptance ("`load_task`
    succeeds on every one of them, no tolerated failures") unmet. Reported to the
    orchestrator rather than silently narrowed.

    Backslash is escaped before `"` -- escaping `"` first and then blindly escaping
    every `\\` would double-escape the backslash that quoting `"` just introduced,
    and would leave any backslash already in the value (`` `re.sub(r"\\d")` ``)
    looking to the YAML scanner like the start of an escape sequence for whatever
    character follows it.
    """
    lines = []
    for line in frontmatter_text.split('\n'):
        match = _TOP_LEVEL_LINE_RE.match(line)
        if match:
            key, value = match.groups()
            if value and value[0] not in '"\'[{|>' and (value.startswith('`') or ': ' in value):
                escaped_value = value.replace('\\', '\\\\').replace('"', '\\"')
                line = f'{key}"{escaped_value}"'
        lines.append(line)
    return '\n'.join(lines)


def _extract_section(body: str, heading: str, source: str) -> str:
    """Return every character after `heading`'s own line up to the next `## ` line.

    No trimming: blank lines and leading/trailing whitespace in the section are kept
    verbatim, per spec 4.3.
    """
    lines = body.splitlines(keepends=True)

    start = None
    for i, line in enumerate(lines):
        if line.rstrip('\n') == heading:
            start = i + 1
            break
    if start is None:
        raise TaskFileError(f"{source}: no '{heading}' section found")

    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith('## '):
            end = i
            break

    return ''.join(lines[start:end])


def _extract_bullets(section_text: str, source: str) -> list[str]:
    """Top-level `- ` bullets in `section_text`; indented continuations join by space."""
    bullets: list[str] = []
    current: list[str] = []

    for line in section_text.splitlines():
        stripped = line.strip()
        if line.startswith('- '):
            if current:
                bullets.append(' '.join(current))
            current = [line[2:].strip()]
        elif current and stripped:
            current.append(stripped)

    if current:
        bullets.append(' '.join(current))

    if not bullets:
        raise TaskFileError(f"{source}: '{_ACCEPTANCE_HEADING}' section has no bullets")

    return bullets
