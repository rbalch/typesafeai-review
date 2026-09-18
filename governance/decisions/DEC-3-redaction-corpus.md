---
id: DEC-3
title: The redactor is measured against a fixed corpus, not the shape in front of its author
status: accepted
kind: negative
created: 2026-09-18
superseded_by: null
controls:
  - path: controls/fitness/redaction_corpus.py
    type: fitness_fn
    enforcement: block
    pragma: supported
---

## Rule

`checks.redact()` must scrub every entry in `controls/fitness/redaction_corpus.py`'s
`MUST_SCRUB` list to the point that the entry's secret substring no longer appears
in the result, and must leave every entry in `MUST_KEEP` byte-for-byte unchanged.
Both lists live in the control, not in this decision, so growing the corpus never
requires touching the decision file.

`MUST_SCRUB` holds one entry per credential shape this project has actually leaked
or nearly leaked: a labelled `token=`/`secret=`/`key=` assignment, an
`Authorization: Bearer` header, a URL's embedded `user:pass@`, an env-style
`KEY=value` line, a YAML/JSON-style `key: "value"`, a bare `ghp_…` GitHub token (the
shape `gh`'s own "Bad credentials" stderr line takes), a bare `sk-…` OpenAI-shaped
key, a bare `AKIA…` AWS access key id, and a PEM private-key header line.

`MUST_KEEP` holds one entry per shape this codebase's own real output produces
today that must never be mistaken for a credential: an `ssh://git@host/...` remote
(no password component), a scp-style `git@host:path` remote, an identifier or
function name that merely contains the substring `key` (`key_name = 'x'`,
`hunk_key(h)`), a token-usage counter (`input_tokens=317`), and a filename
(`keys.json`).

If widening the pattern to catch a new `MUST_SCRUB` entry would also alter a
`MUST_KEEP` entry, the corpus itself is the arbiter: `checks.py` changes until both
lists pass, not the other way around. Adding a new must-scrub shape and a new
must-keep shape in the same commit is expected and encouraged.

## Context

`docs/ledger-findings.md` F-8, three sightings, all the same underlying defect --
the regex is authored to whatever leak shape is directly in front of the author,
never checked against a standing set of shapes it has already promised to handle:

- T-04: `_REDACT_RE` was written to the task's literal pattern
  (`(token|secret|key)\s*[=:]\s*\S+`) and missed `Authorization: Bearer …`, which
  carries no `token`/`secret`/`key` label at all.
- RA-02: the same regex missed a bare `ghp_…` value with no `=`/`:` label --
  exactly how `gh`'s own "Bad credentials" stderr line reads when `fetch_pr` folds
  it into `PRSourceError`.
- RA-04: fixing the URL-credential gap (`://user:pass@`) in round 1 introduced a
  regression in round 2 -- the fix's first pattern also matched a bare
  `ssh://git@host` username with no password, rewriting a factually correct git
  remote in a committed fixture. Two sightings' worth of damage from one change,
  because nothing pinned "this must never be altered" anywhere.

The RA-04 regression is the reason a corpus, not just a wider regex, is the fix.
Widening a pattern to close one gap can silently open another; a fixed set of
must-scrub and must-keep examples, checked together on every change, is the only
way to prove a widening didn't cost something it already had.

## Consequences

`controls/fitness/redaction_corpus.py` imports `typesafe_review.checks.redact` and
runs it over every `MUST_SCRUB` and `MUST_KEEP` entry, failing with the entry's
label, its text, and (for a scrub miss) the secret substring still present, or (for
a keep violation) the altered result.

To close the three known gaps, `checks.py` gained three new patterns in the same
commit as this decision: `_BEARER_RE` (`Authorization: Bearer …`), `_BARE_SECRET_RE`
(`ghp_…` / `sk-…` / `AKIA…` with no label), and `_PEM_RE` (a PEM header line). The
existing `_REDACT_RE` and `_URL_CREDENTIAL_RE` are unchanged.

Red: `checks.py` at the commit before this decision (the `develop` tip it branched
from) fails the control on four corpus entries by name --
`Authorization: Bearer …`, the bare `ghp_…` message, the bare `AKIA…` id, and the
PEM header -- proven by stashing the `checks.py` half of this change and re-running
the control, then restoring it.

Green: the same control after the three new patterns land, plus the full test
suite, proving no existing test relied on the narrower behaviour.

## Rejected alternatives

- **Widen the regex again without a corpus, the same way as the first two
  sightings.** Rejected: this is exactly the pattern with three sightings. A fourth
  ad hoc widening has no reason to fare better than the first three.

- **One giant regex alternation instead of several named patterns.** Rejected: a
  single pattern that has to avoid matching `key_name`, `hunk_key(h)`,
  `input_tokens=317`, and `keys.json` while still catching five unrelated
  credential shapes becomes unreadable and untestable as a unit. Separate,
  narrowly-scoped patterns are each easy to reason about alone; the corpus is what
  proves they compose correctly.

- **A general "looks like a secret" heuristic (entropy scoring, base64-shaped-string
  detection).** Rejected: this is exactly the kind of heuristic that will misfire --
  a git sha, a UUID, or a hash of a fixture body all look high-entropy and are
  none of them secrets. The corpus approach trades "catches everything, sometimes
  wrongly" for "catches exactly what has actually leaked, provably, forever."

- **Enforcement: warn instead of block.** Rejected: the RA-04 regression showed a
  soft check would not have stopped a fix from silently breaking a fixture already
  in the tree. A corpus this cheap to run, that has already caught a real
  regression once, earns `block`.
