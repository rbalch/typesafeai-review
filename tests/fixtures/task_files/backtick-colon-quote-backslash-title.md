---
id: RA-CORPUS-04
plan: run-anywhere
title: `re.sub(r"\d", "")`: strip digits like `say "hi"`
depends_on: []
files:
  - see `src/qux.py` for details
rules: []
---

## Goal

Corpus fixture (RA-02b round 3): every hazard in one title -- a leading backtick, a
`: ` further in, a double quote, and a backslash -- so the escaping order in
`_quote_backtick_values` (backslash first, then `"`) has one fixture that exercises
all of it at once, not just each in isolation.

## Acceptance

- one bullet is enough for this fixture
