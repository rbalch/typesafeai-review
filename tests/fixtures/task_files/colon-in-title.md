---
id: RA-CORPUS-03
plan: run-anywhere
title: Review: `--flag` value
depends_on: []
files:
  - see `src/baz.py` for details
rules: []
---

## Goal

Corpus fixture (RA-02b round 3): a title that does not start with a backtick but
contains `: ` further in, which a YAML parser reads as a nested mapping key unless
quoted -- the `T-04`/`RA-03` shape.

## Acceptance

- one bullet is enough for this fixture
