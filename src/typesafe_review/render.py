"""Render a `Review` (T-08) to `ts-review.md` / `ts-review.json`, and write both
atomically.

Spec §3.1, §7; `reviewer.md`'s two output templates, plus the `## Uncertain` section
this tool adds (§7). Pure formatting only: `compose.py` already decided verdict,
score, severity and findings -- nothing here re-derives any of that.

## `ts-review.md` template

```
# Code Review

- Task source: <task_source>
- Red-sha source: <red_sha_source>

## Verdict

- Verdict: <verdict>
- Score: <score>
- Summary: <summary>

## Required Checks

| Check | Result | Notes |
|---|---|---|
| <name> | <status> | <notes> |

## Findings

### Blocker

- <label> — <issue>
  - Why: <why_it_matters>
  - Fix: <concrete_fix>

### Important
### Minor
### Nit

## Uncertain

- <label> — <issue> (probability: <p>)
  - Why: <why_it_matters>
  - Fix: <concrete_fix>

## Final Notes

None.
```

`<label>` is `file:symbol_or_area` when `Finding.file` is set, else bare
`symbol_or_area` -- never a `None:` prefix. Every empty category (including
`## Uncertain`) renders `None.`. An uncertain finding whose `reason` is
`UNANSWERED` (compose.py) shows `(unanswered)` instead of a probability, since it
never got a model answer at all. `## Final Notes` has no data source in `Review`
itself (notes are pipeline metadata for the JSON only, per the task's non-scope) so
it is always `None.` here.
"""

from __future__ import annotations

import json as json_module
import os
from pathlib import Path
from typing import Any

from typesafe_review.ask import AskResult
from typesafe_review.compose import UNANSWERED, Finding, Review

#: Output file names, the single source `cli.py` imports -- no second literal.
OUTPUT_MD = 'ts-review.md'
OUTPUT_JSON = 'ts-review.json'

_SEVERITY_HEADINGS: tuple[tuple[str, str], ...] = (
    ('blocker', 'Blocker'),
    ('important', 'Important'),
    ('minor', 'Minor'),
    ('nit', 'Nit'),
)


class RenderError(Exception):
    """`write_outputs` failed to leave a consistent pair of output files: a counts
    mismatch caught before any write, or an `OSError` writing or renaming a temp
    file. Wraps the underlying `OSError` as `__cause__` when there is one.
    """


def _label(finding: Finding) -> str:
    if finding.file is None:
        return finding.symbol_or_area
    return f'{finding.file}:{finding.symbol_or_area}'


def _finding_block(finding: Finding, *, suffix: str = '') -> list[str]:
    return [
        f'- {_label(finding)} — {finding.issue}{suffix}',
        f'  - Why: {finding.why_it_matters}',
        f'  - Fix: {finding.concrete_fix}',
    ]


def _uncertain_suffix(finding: Finding) -> str:
    if finding.reason == UNANSWERED:
        return ' (unanswered)'
    if finding.probability is not None:
        return f' (probability: {finding.probability:.2f})'
    return ''


def _table_cell(text: str) -> str:
    """Escape a value for a markdown table cell: no literal pipes, no newlines --
    `CheckResult.notes` is a captured subprocess output tail and can carry both.
    """
    return text.replace('|', '\\|').replace('\n', ' ')


def render_markdown(review: Review, task_source: str, red_sha_source: str) -> str:
    lines: list[str] = [
        '# Code Review',
        '',
        f'- Task source: {task_source}',
        f'- Red-sha source: {red_sha_source}',
        '',
        '## Verdict',
        '',
        f'- Verdict: {review.verdict.value}',
        f'- Score: {review.score}',
        f'- Summary: {review.summary}',
        '',
        '## Required Checks',
        '',
        '| Check | Result | Notes |',
        '|---|---|---|',
    ]
    for check in review.required_checks:
        lines.append(f'| {_table_cell(check.name)} | {_table_cell(check.status)} | {_table_cell(check.notes)} |')
    lines.append('')

    lines.append('## Findings')
    lines.append('')
    for severity, heading in _SEVERITY_HEADINGS:
        lines.append(f'### {heading}')
        lines.append('')
        items = [f for f in review.findings if f.severity == severity]
        if not items:
            lines.append('None.')
        else:
            for finding in items:
                lines.extend(_finding_block(finding))
        lines.append('')

    lines.append('## Uncertain')
    lines.append('')
    if not review.uncertain:
        lines.append('None.')
    else:
        for finding in review.uncertain:
            lines.extend(_finding_block(finding, suffix=_uncertain_suffix(finding)))
    lines.append('')

    lines.append('## Final Notes')
    lines.append('')
    lines.append('None.')
    lines.append('')

    return '\n'.join(lines)


def _finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        'severity': finding.severity,
        'file': finding.file,
        'symbol_or_area': finding.symbol_or_area,
        'issue': finding.issue,
        'why_it_matters': finding.why_it_matters,
        'concrete_fix': finding.concrete_fix,
        'blocks_merge': finding.blocks_merge,
        'question_id': finding.question_id,
        'probability': finding.probability,
        'confidence': finding.confidence,
    }


def render_json(
    review: Review,
    engine: AskResult,
    worktree: Path,
    base: str,
    notes: list[str],
    task_source: str,
    red_sha_source: str,
) -> dict[str, Any]:
    return {
        'verdict': review.verdict.value,
        'score': review.score,
        'summary': review.summary,
        'worktree': str(worktree),
        'base': base,
        'context': {'task_source': task_source, 'red_sha_source': red_sha_source},
        'required_checks': [
            {'name': check.name, 'result': check.status, 'notes': check.notes} for check in review.required_checks
        ],
        'findings': [_finding_to_dict(f) for f in review.findings],
        'uncertain': [_finding_to_dict(f) for f in review.uncertain],
        'counts': dict(review.counts),
        'stop_reason': review.stop_reason,
        'engine': {
            'model': engine.model,
            'requests': engine.requests,
            'input_tokens': engine.input_tokens,
        },
        'notes': list(notes),
    }


def _counts_from_findings(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {severity: 0 for severity, _ in _SEVERITY_HEADINGS}
    for finding in findings:
        counts[finding['severity']] += 1
    return counts


def write_outputs(worktree: Path, md: str, json_obj: dict[str, Any]) -> None:
    """Write `md` / `json_obj` to `OUTPUT_MD` / `OUTPUT_JSON` under `worktree`,
    atomically and only as a pair.

    Both are written to `<name>.tmp` first; only once both temp files are complete
    does `os.replace` swap in the markdown, then the JSON. Any failure -- writing a
    temp file, or renaming either one -- removes both temp files and any final file
    already renamed, then re-raises as `RenderError`. After a failure anywhere,
    neither `ts-review.md` nor `ts-review.json` exists. `counts` is checked against
    the findings array before any file is touched.
    """
    computed_counts = _counts_from_findings(json_obj['findings'])
    if computed_counts != json_obj['counts']:
        raise RenderError(f'counts {json_obj["counts"]!r} do not match findings tally {computed_counts!r}')

    md_path = worktree / OUTPUT_MD
    json_path = worktree / OUTPUT_JSON
    md_tmp = worktree / f'{OUTPUT_MD}.tmp'
    json_tmp = worktree / f'{OUTPUT_JSON}.tmp'

    finals_written: list[Path] = []
    try:
        try:
            md_tmp.write_text(md)
            json_tmp.write_text(json_module.dumps(json_obj, indent=2))
        except OSError as error:
            raise RenderError(f'failed writing temp output files under {worktree}: {error}') from error

        try:
            os.replace(md_tmp, md_path)
            finals_written.append(md_path)
            os.replace(json_tmp, json_path)
            finals_written.append(json_path)
        except OSError as error:
            raise RenderError(f'failed renaming output files under {worktree}: {error}') from error
    except RenderError:
        # Best-effort cleanup: one path failing to unlink must not stop the rest
        # from being attempted, and must not replace the `RenderError` we are
        # already unwinding with a raw `OSError` from here.
        for path in (md_tmp, json_tmp, *finals_written):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                continue
        raise
