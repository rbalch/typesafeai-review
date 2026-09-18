#!/usr/bin/env python3
"""Fitness control: `checks.redact` is measured against a fixed corpus, not
eyeballed against whatever leak shape happened to be in front of the author.

governance: enforces DEC-3

See DEC-3. `docs/ledger-findings.md` F-8 reached its third sighting when the
redaction pattern was widened for one credential shape and immediately clobbered
a benign one (`ssh://git@host` had no password, but the fix-round-1 pattern still
matched its bare username). Both directions of that defect -- missing a real
secret shape, and mangling a benign one -- are the same underlying problem: the
regex was written to whatever was in front of the author, never checked against a
standing corpus.

This control owns that corpus. Every `MUST_SCRUB` entry must come back from
`redact()` with none of its `secret` substring surviving. Every `MUST_KEEP` entry
must come back byte-for-byte unchanged. A regex change that fixes one entry and
breaks another shows up here, in one run, instead of in the next incident report.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / 'src'
sys.path.insert(0, str(SRC_DIR))

from typesafe_review.checks import redact

# Each entry: (label, full text handed to redact(), the secret substring that must
# not survive). The label documents which real-world shape the entry stands in
# for; see DEC-3 Context for where each one came from.
MUST_SCRUB: list[tuple[str, str, str]] = [
    (
        'token=<value>, F-8 original shape',
        'token=ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ',
        'ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ',
    ),
    (
        'Authorization: Bearer header, F-8 T-04 sighting 1',
        'Authorization: Bearer abc123.def456-XYZ',
        'abc123.def456-XYZ',
    ),
    ('URL embedded user:pass, F-8 T-04', 'https://u:p@host/org/repo.git', 'u:p@'),
    (
        'env-style KEY=value, TypeSafe SDK env var',
        'TYPESAFE_API_KEY=sk-abcdefghijklmnopqrstuvwx',
        'sk-abcdefghijklmnopqrstuvwx',
    ),
    ('yaml/json-style key: "value"', 'api_key: "abcdefghijklmnopqrstuvwx"', 'abcdefghijklmnopqrstuvwx'),
    (
        'bare GitHub token, F-8 RA-02 sighting 2 (gh Bad credentials)',
        'Bad credentials: ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ',
        'ghp_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ',
    ),
    (
        'bare OpenAI-shaped key',
        'export OPENAI_KEY=sk-abcdefghijklmnopqrstuvwxyzABCDEF',
        'sk-abcdefghijklmnopqrstuvwxyzABCDEF',
    ),
    ('bare AWS access key id', 'aws_access_key_id AKIAIOSFODNN7EXAMPLE', 'AKIAIOSFODNN7EXAMPLE'),
    ('PEM private key header', '-----BEGIN RSA PRIVATE KEY-----', 'RSA PRIVATE KEY'),
]

# Each entry: (label, text handed to redact()) that must survive identical. Every
# one is a shape this codebase's own output produces today; see DEC-3 Context.
MUST_KEEP: list[tuple[str, str]] = [
    ('ssh remote URL, F-8 fix-round-2 regression', 'ssh://git@host/o/r.git'),
    ('scp-style git remote', 'git@host:o/r.git'),
    ('identifier merely containing "key"', "key_name = 'x'"),
    ('function name merely containing "key"', 'hunk_key(h)'),
    ('token usage counter, not a secret', 'input_tokens=317'),
    ('filename merely containing "key"', 'keys.json'),
]


def find_violations() -> list[str]:
    violations: list[str] = []

    for label, text, secret in MUST_SCRUB:
        result = redact(text)
        if secret in result:
            violations.append(
                f'must-scrub corpus entry {label!r} ({text!r}) was not scrubbed: '
                f'{secret!r} still present in {result!r} (DEC-3)'
            )

    for label, text in MUST_KEEP:
        result = redact(text)
        if result != text:
            violations.append(f'must-keep corpus entry {label!r} ({text!r}) was altered: got {result!r} (DEC-3)')

    return violations


def main() -> int:
    violations = find_violations()
    if violations:
        for v in violations:
            print(f'FAIL [DEC-3] {v}', file=sys.stderr)
        print(
            '    -> checks.redact() must scrub every DEC-3 must-scrub entry and leave every '
            'must-keep entry untouched. Widen or narrow the pattern in checks.py to fit the '
            'whole corpus, then run `uv run pytest` to confirm nothing else relied on the old '
            'shape.',
            file=sys.stderr,
        )
        return 1

    print(
        f'ok [DEC-3] checks.redact() scrubs all {len(MUST_SCRUB)} must-scrub entries and '
        f'leaves all {len(MUST_KEEP)} must-keep entries untouched.'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
