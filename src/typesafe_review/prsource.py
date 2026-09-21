"""Load a `--task` brief straight from a PR the orchestrate flow made, instead of a
task file on disk (RA-02).

`orchestrate`'s squash step writes each task brief verbatim into the PR body, inside a
`<details><summary>Task brief …</summary> … </details>` block, so a review of any PR
that flow produced needs no task file checked out locally. `cli.py` resolves
`--task 13` / `--task #13` / a PR URL to a number with `parse_pr_ref`, fetches the PR
with `fetch_pr`, and pulls the brief and (best effort) the red-sha out of the body with
`extract_brief` / `extract_red_sha`.

`prsource.py` declares its own `PRSourceError`; DEC-1 covers its one `subprocess.run`
call (`fetch_pr`), wrapped in its own `try`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from typesafe_review.checks import redact

#: `13`, `#13`, or a GitHub PR URL, optionally with a trailing path/query/fragment.
_PR_URL_RE = re.compile(r'^https://github\.com/[^/\s]+/[^/\s]+/pull/(\d+)(?:[/?#].*)?$')

#: `orchestrate`'s squash step (SKILL.md step 3) appends the whole task file verbatim
#: as the *last* thing in the PR body, inside one `<details><summary>Task brief
#: <id></summary> … </details>` block -- there is never more than one such block, and
#: nothing follows it. Greedy so a brief whose own prose mentions `</details>` (this
#: task's brief does, and RA-02's does) does not truncate the match at that inner
#: occurrence: this regex always runs to the *last* `</details>` in the body, which is
#: the real closing tag.
_BRIEF_RE = re.compile(r'<details>\s*<summary>\s*Task brief[^<]*</summary>(.*)</details>', re.DOTALL)

#: An opening fence line: ``` optionally followed by an info string (e.g. `markdown`).
_FENCE_OPEN_RE = re.compile(r'^```[^\n`]*$')

#: A closing fence line: bare ``` with nothing else on the line.
_FENCE_CLOSE_RE = re.compile(r'^```$')

#: `Red: <sha>` as a structured line (RA-05 adds it to the PR template; not written
#: yet). Multiline so `^` matches the start of any line, not just the string start.
_RED_SHA_STRUCTURED_RE = re.compile(r'^Red:\s*([0-9a-f]{7,40})', re.MULTILINE)

#: The prose form today's PR bodies actually carry ("Evidence: ... red-then-green on
#: <sha>, ...").
_RED_SHA_PROSE_RE = re.compile(r'red-then-green on ([0-9a-f]{7,40})')

_GH_JSON_FIELDS = 'number,title,body,baseRefName,headRefOid,mergeCommit,state'


class PRSourceError(Exception):
    """`gh` is missing, not authenticated, the repo has no GitHub remote, the PR
    could not be fetched or parsed, or the PR body has no `Task brief` block."""


@dataclass(frozen=True)
class PullRequest:
    """The subset of `gh pr view --json ...` this reviewer needs."""

    number: int
    title: str
    body: str
    base_ref: str
    head_sha: str
    merge_commit: str | None
    state: str


def parse_pr_ref(value: str) -> int | None:
    """`13`, `#13`, or a GitHub PR URL -> its number. Anything else -> `None`, so
    `--task` falls back to treating `value` as a file path."""
    stripped = value.strip().removeprefix('#')
    if stripped.isdigit():
        return int(stripped)

    match = _PR_URL_RE.match(value.strip())
    if match:
        return int(match.group(1))

    return None


def fetch_pr(number: int, repo_dir: Path) -> PullRequest:
    """`gh pr view <number> --json ...` run with `cwd=repo_dir`. `gh` missing, not
    authed, no GitHub remote, or a non-zero exit all raise `PRSourceError` carrying
    `gh`'s own stderr."""
    try:
        result = subprocess.run(
            ['gh', 'pr', 'view', str(number), '--json', _GH_JSON_FIELDS],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        raise PRSourceError(f'gh pr view {number}: could not run `gh` ({e})') from e

    if result.returncode != 0:
        stderr_tail = redact('\n'.join(result.stderr.strip().splitlines()[-5:]))
        raise PRSourceError(f'gh pr view {number} failed: {stderr_tail}')

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise PRSourceError(f'gh pr view {number}: could not parse `gh` output as JSON ({e})') from e

    merge_commit = data.get('mergeCommit')
    merge_sha = merge_commit.get('oid') if isinstance(merge_commit, dict) else merge_commit

    try:
        return PullRequest(
            number=data['number'],
            title=data['title'],
            body=data['body'],
            base_ref=data['baseRefName'],
            head_sha=data['headRefOid'],
            merge_commit=merge_sha,
            state=data['state'],
        )
    except KeyError as e:
        raise PRSourceError(f'gh pr view {number}: response is missing field {e}') from e


def extract_brief(body: str) -> str:
    """The text inside the first `<details><summary>Task brief …</summary> …
    </details>` block, leading/trailing blank lines trimmed. No block -> `PRSourceError`.

    PRs #1-#12 additionally wrapped the brief in a single outer ```` ``` ```` fence
    (optionally ```` ```markdown ````) inside that block; #13 onward paste it bare
    (RA-02b). If the trimmed text starts with a fence line, that fence must also be
    the last line, or the fence was never closed and the brief is malformed -> both
    fence lines are stripped and the text between them (verbatim) is the brief.
    """
    match = _BRIEF_RE.search(body)
    if match is None:
        raise PRSourceError('PR body has no <details><summary>Task brief …</summary> … </details> block')
    brief = match.group(1).strip()

    lines = brief.split('\n')
    if _FENCE_OPEN_RE.match(lines[0]):
        if len(lines) < 2 or not _FENCE_CLOSE_RE.match(lines[-1]):
            raise PRSourceError('Task brief block opens a ``` fence that is never closed')
        brief = '\n'.join(lines[1:-1]).strip()

    return brief


def extract_red_sha(body: str) -> str | None:
    """First match of the structured `Red: <sha>` line, else the prose
    `red-then-green on <sha>` form. Best effort: `None` if neither is present."""
    match = _RED_SHA_STRUCTURED_RE.search(body)
    if match:
        return match.group(1)

    match = _RED_SHA_PROSE_RE.search(body)
    if match:
        return match.group(1)

    return None
